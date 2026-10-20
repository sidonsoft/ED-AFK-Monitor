"""Bounded background delivery; importing this module has no side effects."""
import json
import socket
import math
from ssl import SSLCertVerificationError
from dataclasses import dataclass
from queue import Queue, Empty, Full
from threading import Thread, Event
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


@dataclass(frozen=True)
class LiveUpdate:
    text: str
    generation: int

    def __str__(self):
        return self.text


@dataclass(frozen=True)
class Delivery:
    """One queued notification; the id names its saved copy, if any."""
    id: str
    payload: object


class DeliveryError(Exception):
    def __init__(self, message, retryable=False, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def check_response(status, retry_after=None):
    if not 200 <= status < 300:
        try:
            delay = float(retry_after) if retry_after is not None else None
            if delay is not None and (not math.isfinite(delay) or delay < 0):
                delay = None
        except (TypeError, ValueError):
            delay = None
        raise DeliveryError(f"HTTP {status}", status == 429 or status >= 500, delay)


CERTIFICATE_ERROR = "TLS certificate verification failed; repair Python CA certificates or set SSL_CERT_FILE to a trusted CA bundle"


def telegram_send(token, chat_id, message, timeout=5):
    # Plain text deliberately avoids interpreting Discord markup or user-supplied HTML.
    request = Request(f"https://api.telegram.org/bot{token}/sendMessage",
                      data=json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except HTTPError as error:
        delay = error.headers.get("Retry-After") if error.headers else None
        try:
            payload = json.loads(error.read())
            parameters = payload.get("parameters") if isinstance(payload, dict) else None
            if isinstance(parameters, dict):
                delay = parameters.get("retry_after", delay)
        except (ValueError, OSError):
            pass
        finally:
            error.close()
        check_response(error.code, delay)
    except SSLCertVerificationError:
        raise DeliveryError(CERTIFICATE_ERROR) from None
    except URLError as error:
        if isinstance(error.reason, SSLCertVerificationError):
            raise DeliveryError(CERTIFICATE_ERROR) from None
        raise DeliveryError("connection failed or timed out", retryable=True) from None
    except (TimeoutError, socket.timeout, OSError):
        # Exception strings can contain the bot token in the request URL.
        raise DeliveryError("connection failed or timed out", retryable=True) from None
    except ValueError:
        raise DeliveryError("invalid response") from None
    else:
        if not isinstance(data, dict):
            raise DeliveryError("invalid response")
        parameters = data.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        if data.get("ok") is not True:
            check_response(data.get("error_code", 500), parameters.get("retry_after"))
            raise DeliveryError("API rejected message")


class NotificationDispatcher:
    """One independent FIFO worker per channel; all terminal output stays on the caller."""
    def __init__(self, senders, preview=False, capacity=200, attempts=3, outbox=None):
        self.senders = senders
        self.preview = preview
        self.attempts = attempts
        self.reports = Queue()
        self.stopped = Event()
        self.closed = False
        self.queues = {name: Queue(capacity) for name in senders}
        self.outbox = None
        pending = {}
        if not preview and outbox is not None:
            try:
                pending, warnings = outbox.load(limit=capacity)
            except Exception:
                self.reports.put("Notifications: saved-message store unavailable; continuing without it")
            else:
                self.outbox = outbox
                for warning in warnings:
                    self.reports.put(warning)
        self.workers = []
        if not preview:
            for name in senders:
                worker = Thread(target=self._run, args=(name,), daemon=True)
                worker.start()
                self.workers.append(worker)
        replayed = 0
        for channel, entries in pending.items():
            if channel not in self.queues:
                continue  # Keep saved messages until that channel is configured again.
            for entry in entries:
                try:
                    self.queues[channel].put_nowait(Delivery(entry["id"], entry["text"]))
                except Full:
                    self.reports.put(f"{channel}: notification queue full; saved messages remain")
                    break
                replayed += 1
        if replayed:
            self.reports.put(f"Notifications: replaying {replayed} saved message(s)")

    def submit(self, channel, message):
        if self.closed or channel not in self.senders or not message:
            return False
        if self.preview:
            self.reports.put(f"{channel.upper()} PREVIEW: {message}")
            return True
        delivery = Delivery(uuid4().hex, message)
        outbox = self.outbox
        saved = False
        if outbox is not None and channel in outbox.channels and isinstance(message, str):
            saved = self._save(outbox, channel, delivery)
        try:
            self.queues[channel].put_nowait(delivery)
            return True
        except Full:
            suffix = "; saved for next start" if saved else ""
            self.reports.put(f"{channel}: notification queue full; message not queued{suffix}")
            return False

    def _save(self, outbox, channel, delivery):
        try:
            outbox.add(channel, delivery.id, delivery.payload)
            return True
        except Exception:
            self.reports.put("Notifications: could not save pending messages; continuing without the store")
            self.outbox = None
            return False

    def _complete(self, channel, delivery_id):
        outbox = self.outbox
        if outbox is None:
            return
        try:
            outbox.complete(channel, delivery_id)
        except Exception:
            self.reports.put("Notifications: could not update saved messages")
            self.outbox = None

    def _saved_channel(self, channel):
        outbox = self.outbox
        return outbox is not None and channel in outbox.channels

    def _wait_until(self, deadline):
        # Split very large server delays into portable, interruptible waits.
        while not self.stopped.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            if self.stopped.wait(min(remaining, 60)):
                return True
        return True

    def _run(self, channel):
        queue = self.queues[channel]
        next_send_at = 0
        while not self.stopped.is_set():
            try:
                delivery = queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                for attempt in range(self.attempts):
                    if self._wait_until(next_send_at):
                        break
                    try:
                        self.senders[channel](delivery.payload)
                        self._complete(channel, delivery.id)
                        break
                    except DeliveryError as error:
                        if error.retryable or error.retry_after is not None:
                            delay = error.retry_after if error.retry_after is not None else 2 ** attempt
                            next_send_at = monotonic() + delay
                        if not error.retryable or attempt + 1 == self.attempts:
                            if not error.retryable:
                                self._complete(channel, delivery.id)
                                self.reports.put(f"{channel}: delivery failed ({error})")
                            elif self._saved_channel(channel):
                                self.reports.put(f"{channel}: delivery failed ({error}); kept for next start")
                            else:
                                self.reports.put(f"{channel}: delivery failed ({error})")
                            break
                    except Exception:
                        self._complete(channel, delivery.id)
                        self.reports.put(f"{channel}: delivery failed (unexpected transport error)")
                        break
            finally:
                queue.task_done()

    def drain_reports(self):
        while True:
            try:
                yield self.reports.get_nowait()
            except Empty:
                return

    def close(self, timeout=6):
        if self.closed:
            return
        self.closed = True
        deadline = monotonic() + timeout
        while any(q.unfinished_tasks for q in self.queues.values()) and monotonic() < deadline:
            self.stopped.wait(0.02)
        pending = sum(q.unfinished_tasks for q in self.queues.values())
        self.stopped.set()
        for worker in self.workers:
            worker.join(timeout=max(0, deadline - monotonic()))
        if pending:
            suffix = " (saved for next start)" if self.outbox is not None else ""
            self.reports.put(f"Notifications: stopped with {pending} pending delivery(s){suffix}")
