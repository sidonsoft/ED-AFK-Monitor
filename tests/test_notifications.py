import io
import json
from pathlib import Path
from ssl import SSLCertVerificationError
from tempfile import TemporaryDirectory
import unittest
from threading import Event
from time import monotonic, sleep
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from notifications import (DeliveryError, LiveUpdate, NotificationDispatcher, check_response,
                       split_message, telegram_send)
from outbox import Outbox


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
            self.assertEqual(caught.exception.retry_after, 999)

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

    def test_invalid_retry_delays_use_default_backoff(self):
        for value in ('nan', 'inf', '-1', 'invalid'):
            with self.subTest(value=value), self.assertRaises(DeliveryError) as caught:
                check_response(429, value)
            self.assertIsNone(caught.exception.retry_after)

    def test_long_rate_limit_can_be_interrupted_at_shutdown(self):
        entered = Event()
        calls = []
        def send(message):
            calls.append(message)
            entered.set()
            raise DeliveryError('HTTP 429', True, 999)
        dispatcher = NotificationDispatcher({'telegram': send})
        dispatcher.submit('telegram', 'one')
        self.assertTrue(entered.wait(1))
        before = monotonic()
        dispatcher.close(timeout=0.01)
        for worker in dispatcher.workers:
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        self.assertLess(monotonic() - before, 1)
        self.assertEqual(calls, ['one'])
        self.assertFalse(dispatcher.submit('telegram', 'after close'))
        self.assertIn('pending', next(dispatcher.drain_reports()))

    def test_retry_delay_applies_to_next_message_after_attempts_exhausted(self):
        calls = []
        def send(message):
            calls.append((message, monotonic()))
            if message == 'one':
                raise DeliveryError('HTTP 429', True, 0.03)
        dispatcher = NotificationDispatcher({'telegram': send}, attempts=1)
        dispatcher.submit('telegram', 'one')
        dispatcher.submit('telegram', 'two')
        dispatcher.close()
        self.assertEqual([message for message, _ in calls], ['one', 'two'])
        self.assertGreaterEqual(calls[1][1] - calls[0][1], 0.025)

    def test_non_object_error_body_preserves_retryable_http_status(self):
        error = HTTPError('https://example.invalid', 503, 'unavailable', {}, io.BytesIO(b'[]'))
        with patch('notifications.urlopen', side_effect=error):
            with self.assertRaises(DeliveryError) as caught:
                telegram_send('fake', 1, 'hi')
        self.assertTrue(caught.exception.retryable)
        self.assertIn('HTTP 503', str(caught.exception))

    def test_invalid_success_payload_is_reported_without_leaking_content(self):
        for payload, retryable in [(b'null', False), (b'[]', False),
                                   (b'{"ok":false,"error_code":429,"parameters":[]}', True)]:
            with self.subTest(payload=payload):
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.read.return_value = payload
                with patch('notifications.urlopen', return_value=response):
                    with self.assertRaises(DeliveryError) as caught:
                        telegram_send('SECRET', 1, 'hi')
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn('SECRET', str(caught.exception))


class SavedNotificationTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=2):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if predicate():
                return True
            sleep(0.01)
        return predicate()

    def pending_texts(self, folder, channel="telegram"):
        return [entry["text"] for entry in Outbox(Path(folder), [channel]).load()[0][channel]]

    def test_saved_message_is_replayed_and_removed_by_the_next_dispatcher(self):
        with TemporaryDirectory() as folder:
            Outbox(Path(folder), ["telegram"]).add("telegram", "saved", "hull damaged")
            delivered = []
            dispatcher = NotificationDispatcher({"telegram": delivered.append},
                                                outbox=Outbox(Path(folder), ["telegram"]))
            try:
                self.assertTrue(self.wait_for(lambda: delivered == ["hull damaged"]))
            finally:
                dispatcher.close()
            self.assertEqual(delivered, ["hull damaged"])
            self.assertEqual(self.pending_texts(folder), [])
            self.assertTrue(any("replaying 1 saved message" in report
                                for report in dispatcher.drain_reports()))

    def test_message_is_saved_before_delivery_and_removed_after_success(self):
        with TemporaryDirectory() as folder:
            started, release = Event(), Event()
            def sender(message):
                started.set()
                release.wait(2)
            dispatcher = NotificationDispatcher({"telegram": sender},
                                                outbox=Outbox(Path(folder), ["telegram"]))
            try:
                dispatcher.submit("telegram", "hull damaged")
                self.assertTrue(started.wait(1))
                self.assertEqual(self.pending_texts(folder), ["hull damaged"])
            finally:
                release.set()
                dispatcher.close()
            self.assertEqual(self.pending_texts(folder), [])

    def test_permanent_failure_is_not_kept_for_next_start(self):
        with TemporaryDirectory() as folder:
            dispatcher = NotificationDispatcher(
                {"telegram": Mock(side_effect=DeliveryError("HTTP 403"))},
                outbox=Outbox(Path(folder), ["telegram"]))
            dispatcher.submit("telegram", "rejected")
            dispatcher.close()
            reports = list(dispatcher.drain_reports())
            self.assertTrue(any("HTTP 403" in report for report in reports))
            self.assertFalse(any("kept" in report for report in reports))
            self.assertEqual(self.pending_texts(folder), [])

    def test_transient_failure_is_kept_for_next_start(self):
        with TemporaryDirectory() as folder:
            dispatcher = NotificationDispatcher(
                {"telegram": Mock(side_effect=DeliveryError("HTTP 503", True))},
                attempts=1, outbox=Outbox(Path(folder), ["telegram"]))
            dispatcher.submit("telegram", "retry later")
            dispatcher.close()
            self.assertTrue(any("kept for next start" in report
                                for report in dispatcher.drain_reports()))
            self.assertEqual(self.pending_texts(folder), ["retry later"])

    def test_live_updates_are_never_saved(self):
        with TemporaryDirectory() as folder:
            dispatcher = NotificationDispatcher({"discord_live": Mock()},
                                                outbox=Outbox(Path(folder), ["discord"]))
            dispatcher.submit("discord_live", LiveUpdate("live stats", 1))
            dispatcher.close()
            self.assertFalse((Path(folder) / "discord.json").exists())

    def test_preview_never_touches_the_store(self):
        outbox = Mock()
        dispatcher = NotificationDispatcher({"telegram": Mock()}, preview=True, outbox=outbox)
        dispatcher.submit("telegram", "preview only")
        dispatcher.close()
        outbox.load.assert_not_called()
        outbox.add.assert_not_called()
        self.assertEqual(len(list(dispatcher.drain_reports())), 1)

    def test_store_write_failure_does_not_stop_delivery(self):
        class BrokenOutbox:
            channels = ("telegram",)
            def load(self, limit=None):
                return {"telegram": []}, []
            def add(self, *args):
                raise OSError("disk full")
            def complete(self, *args):
                pass

        delivered = []
        dispatcher = NotificationDispatcher({"telegram": delivered.append}, outbox=BrokenOutbox())
        dispatcher.submit("telegram", "still delivered")
        dispatcher.submit("telegram", "and again")
        dispatcher.close()
        self.assertEqual(delivered, ["still delivered", "and again"])
        reports = list(dispatcher.drain_reports())
        self.assertEqual(sum("could not save" in report for report in reports), 1)

    def test_unavailable_store_still_delivers(self):
        class BrokenOutbox:
            channels = ("telegram",)
            def load(self, limit=None):
                raise OSError("no access")

        delivered = []
        dispatcher = NotificationDispatcher({"telegram": delivered.append}, outbox=BrokenOutbox())
        dispatcher.submit("telegram", "delivered anyway")
        dispatcher.close()
        self.assertEqual(delivered, ["delivered anyway"])
        self.assertTrue(any("store unavailable" in report
                            for report in dispatcher.drain_reports()))


class MessageLengthTests(unittest.TestCase):
    def test_split_prefers_newline_boundaries_and_round_trips(self):
        text = "\n".join(f"line {i} payload padding" for i in range(20))
        chunks = split_message(text, 200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_split_force_splits_oversized_single_line(self):
        text = "x" * 4500
        chunks = split_message(text, 4096)
        self.assertEqual([len(chunk) for chunk in chunks], [4096, 404])
        self.assertEqual("".join(chunks), text)

    def test_split_no_op_under_limit_or_without_limit(self):
        self.assertEqual(split_message("short", 2000), ["short"])
        self.assertEqual(split_message("x" * 5000, 0), ["x" * 5000])

    def test_dispatcher_chunks_per_channel_limit_and_completes_outbox(self):
        with TemporaryDirectory() as directory:
            calls = []
            outbox = Outbox(Path(directory) / "pending", ["discord"])
            dispatcher = NotificationDispatcher({"discord": calls.append}, outbox=outbox,
                                                max_lengths={"discord": 100})
            text = "\n".join(["a" * 40] * 10)
            self.assertTrue(dispatcher.submit("discord", text))
            dispatcher.close()
            self.assertGreater(len(calls), 1)
            self.assertTrue(all(len(call) <= 100 for call in calls))
            self.assertEqual("\n".join(calls), text)
            pending, warnings = outbox.load()
            self.assertEqual(warnings, [])
            self.assertEqual(pending["discord"], [])

    def test_live_update_objects_are_never_split(self):
        calls = []
        dispatcher = NotificationDispatcher({"discord_live": calls.append}, max_lengths={"discord_live": 10})
        update = LiveUpdate("line one\nline two long enough to exceed the tiny limit", 1)
        dispatcher.submit("discord_live", update)
        dispatcher.close()
        self.assertEqual(calls, [update])
