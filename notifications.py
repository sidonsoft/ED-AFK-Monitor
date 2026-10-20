"""Bounded background delivery; importing this module has no side effects."""
import json
import socket
from dataclasses import dataclass
from queue import Queue, Empty, Full
from threading import Thread, Event
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class LiveUpdate:
    text: str
    generation: int

    def __str__(self):
        return self.text


class DeliveryError(Exception):
    def __init__(self, message, retryable=False, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def check_response(status, retry_after=None):
    if not 200 <= status < 300:
        try:
            delay = max(0, min(float(retry_after), 30)) if retry_after is not None else None
        except (TypeError, ValueError):
            delay = None
        raise DeliveryError(f"HTTP {status}", status == 429 or status >= 500, delay)


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
            delay = json.loads(error.read()).get("parameters", {}).get("retry_after", delay)
        except (ValueError, OSError):
            pass
        check_response(error.code, delay)
    except (URLError, TimeoutError, socket.timeout, OSError):
        # Exception strings can contain the bot token in the request URL.
        raise DeliveryError("connection failed or timed out", retryable=True) from None
    except ValueError:
        raise DeliveryError("invalid response") from None
    else:
        if not data.get("ok"):
            check_response(data.get("error_code", 500), data.get("parameters", {}).get("retry_after"))
            raise DeliveryError("API rejected message")


class NotificationDispatcher:
    """One independent FIFO worker per channel; all terminal output stays on the caller."""
    def __init__(self, senders, preview=False, capacity=200, attempts=3):
        self.senders = senders
        self.preview = preview
        self.attempts = attempts
        self.reports = Queue()
        self.stopped = Event()
        self.closed = False
        self.queues = {name: Queue(capacity) for name in senders}
        self.workers = []
        if not preview:
            for name in senders:
                worker = Thread(target=self._run, args=(name,), daemon=True)
                worker.start()
                self.workers.append(worker)

    def submit(self, channel, message):
        if self.closed or channel not in self.senders or not message:
            return False
        if self.preview:
            self.reports.put(f"{channel.upper()} PREVIEW: {message}")
            return True
        try:
            self.queues[channel].put_nowait(message)
            return True
        except Full:
            self.reports.put(f"{channel}: notification queue full; message not queued")
            return False

    def _run(self, channel):
        queue = self.queues[channel]
        while not self.stopped.is_set():
            try:
                message = queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                for attempt in range(self.attempts):
                    try:
                        self.senders[channel](message)
                        break
                    except DeliveryError as error:
                        if not error.retryable or attempt + 1 == self.attempts:
                            self.reports.put(f"{channel}: delivery failed ({error})")
                            break
                        delay = error.retry_after if error.retry_after is not None else 2 ** attempt
                        if self.stopped.wait(delay):
                            break
                    except Exception:
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
            self.reports.put(f"Notifications: stopped with {pending} pending delivery(s)")
