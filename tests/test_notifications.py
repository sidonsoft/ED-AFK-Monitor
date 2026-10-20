import io
import json
from ssl import SSLCertVerificationError
import unittest
from threading import Event
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from notifications import DeliveryError, NotificationDispatcher, check_response, telegram_send


class NotificationTests(unittest.TestCase):
    def test_preview_never_invokes_any_transport(self):
        send = Mock()
        dispatcher = NotificationDispatcher({"discord": send, "telegram": send, "discord_live": send}, preview=True)
        for channel in dispatcher.senders:
            self.assertTrue(dispatcher.submit(channel, "hull damage"))
        dispatcher.close()
        send.assert_not_called()
        self.assertEqual(len(list(dispatcher.drain_reports())), 3)

    def test_slow_channel_does_not_hold_up_other_channel_or_caller(self):
        release = Event()
        entered = Event()
        delivered = Event()
        def slow(message):
            entered.set()
            release.wait(2)
        dispatcher = NotificationDispatcher({"discord": slow, "telegram": lambda _: delivered.set()})
        try:
            dispatcher.submit("discord", "one")
            self.assertTrue(entered.wait(1))
            dispatcher.submit("telegram", "two")
            self.assertTrue(delivered.wait(1))
        finally:
            release.set()
            dispatcher.close()

    def test_transient_failure_retries_and_preserves_order(self):
        calls = []
        def send(message):
            calls.append(message)
            if len(calls) == 1:
                raise DeliveryError("HTTP 429", True, 0)
        dispatcher = NotificationDispatcher({"telegram": send})
        dispatcher.submit("telegram", "one")
        dispatcher.submit("telegram", "two")
        dispatcher.close()
        self.assertEqual(calls, ["one", "one", "two"])
        self.assertEqual(list(dispatcher.drain_reports()), [])

    def test_permanent_failure_is_not_retried(self):
        send = Mock(side_effect=DeliveryError("HTTP 403"))
        dispatcher = NotificationDispatcher({"telegram": send})
        dispatcher.submit("telegram", "one")
        dispatcher.close()
        self.assertEqual(send.call_count, 1)
        self.assertIn("HTTP 403", next(dispatcher.drain_reports()))

    def test_retry_count_is_bounded(self):
        send = Mock(side_effect=DeliveryError("HTTP 503", True, 0))
        dispatcher = NotificationDispatcher({"telegram": send})
        dispatcher.submit("telegram", "one")
        dispatcher.close()
        self.assertEqual(send.call_count, 3)
        self.assertIn("delivery failed", next(dispatcher.drain_reports()))

    def test_full_queue_is_reported_without_blocking(self):
        entered, release = Event(), Event()
        def send(message):
            entered.set()
            release.wait(2)
        dispatcher = NotificationDispatcher({"telegram": send}, capacity=1)
        try:
            dispatcher.submit("telegram", "one")
            self.assertTrue(entered.wait(1))
            self.assertTrue(dispatcher.submit("telegram", "two"))
            self.assertFalse(dispatcher.submit("telegram", "three"))
            self.assertIn("queue full", next(dispatcher.drain_reports()))
        finally:
            release.set()
            dispatcher.close()

    def test_timeout_exception_does_not_expose_bot_token(self):
        with patch("notifications.urlopen", side_effect=URLError("https://api.telegram.org/botSECRET")):
            with self.assertRaises(DeliveryError) as error:
                telegram_send("SECRET", 123, "hello")
        self.assertNotIn("SECRET", str(error.exception))
        self.assertTrue(error.exception.retryable)

    def test_telegram_posts_plain_text_with_timeout(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok":true}'
        with patch("notifications.urlopen", return_value=response) as opener:
            telegram_send("fake", -123, "⚠ Hull <80%>")
        request = opener.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"chat_id": -123, "text": "⚠ Hull <80%>"})
        self.assertEqual(opener.call_args.kwargs["timeout"], 5)

    def test_telegram_rate_limit_respects_server_delay(self):
        error = HTTPError("https://example.invalid", 429, "limit", {}, io.BytesIO(b'{"parameters":{"retry_after":7}}'))
        with patch("notifications.urlopen", side_effect=error):
            with self.assertRaises(DeliveryError) as caught:
                telegram_send("fake", 1, "hi")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.retry_after, 7)

    def test_http_status_classification(self):
        for status, retryable in [(400, False), (403, False), (429, True), (500, True)]:
            with self.subTest(status=status), self.assertRaises(DeliveryError) as caught:
                check_response(status, "999")
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertEqual(caught.exception.retry_after, 30)

    def test_certificate_errors_are_actionable_and_not_retried(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                certificate_error = SSLCertVerificationError(1, 'certificate failure SECRET')
                error = URLError(certificate_error) if wrapped else certificate_error
                with patch('notifications.urlopen', side_effect=error) as opener:
                    dispatcher = NotificationDispatcher({'telegram': lambda message: telegram_send('SECRET', 1, message)})
                    dispatcher.submit('telegram', 'test')
                    dispatcher.close()
                self.assertEqual(opener.call_count, 1)
                report = next(dispatcher.drain_reports())
                self.assertIn('SSL_CERT_FILE', report)
                self.assertIn('certificate verification failed', report)
                self.assertNotIn('SECRET', report)
