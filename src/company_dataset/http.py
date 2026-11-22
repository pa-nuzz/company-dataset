import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

USER_AGENT = "company-dataset/1.0 (open dataset builder; polite; identifies itself)"


class RequestError(Exception):
    pass


@dataclass
class HttpResponse:
    status: int
    text: str
    url: str
    content: bytes = b""

    def json(self):
        import json

        return json.loads(self.text)


class _HostThrottle:
    def __init__(self, interval: float, sleep):
        self.interval = interval
        self.sleep = sleep
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delta = self.last + self.interval - now
            if delta > 0:
                self.sleep(delta)
            self.last = time.monotonic()


def _decode(resp) -> str:
    """Response text, preferring UTF-8 when the server didn't declare a charset.

    requests falls back to ISO-8859-1 for text/* without a charset, which turns
    "gestión" into "gestiÃ³n" on the many sites that serve UTF-8 without saying so.
    """
    content = getattr(resp, "content", None)
    declared = "charset=" in ((getattr(resp, "headers", None) or {}).get("Content-Type", "") or "").lower()
    if isinstance(content, bytes) and not declared:
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return resp.text


class HttpClient:
    RETRYABLE = {403, 429, 500, 502, 503, 504}

    def __init__(
        self,
        session=None,
        timeout: float = 20.0,
        max_attempts: int = 3,
        per_host_interval: float = 0.15,
        sleep=time.sleep,
        backoff_base: float = 1.5,
    ):
        self.session = session or requests.Session()
        if isinstance(self.session, requests.Session):
            self.session.headers["User-Agent"] = USER_AGENT
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.per_host_interval = per_host_interval
        self.sleep = sleep
        self.backoff_base = backoff_base
        self._throttles: dict[str, _HostThrottle] = {}
        self._guard = threading.Lock()

    def _throttle(self, url: str) -> _HostThrottle:
        host = urlsplit(url).hostname or ""
        with self._guard:
            throttle = self._throttles.get(host)
            if throttle is None:
                throttle = _HostThrottle(self.per_host_interval, self.sleep)
                self._throttles[host] = throttle
            return throttle

    def get(self, url: str, headers: dict | None = None, binary: bool = False) -> HttpResponse:
        throttle = self._throttle(url)
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            throttle.wait()
            try:
                if headers:
                    resp = self.session.get(url, timeout=self.timeout, headers=headers)
                else:
                    resp = self.session.get(url, timeout=self.timeout)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    self._backoff(attempt)
                    continue
                raise RequestError(str(exc)) from exc
            status = resp.status_code
            if status in self.RETRYABLE and attempt < self.max_attempts:
                self._backoff(attempt)
                continue
            if binary:
                return HttpResponse(status, "", resp.url, getattr(resp, "content", b""))
            return HttpResponse(status, _decode(resp), resp.url)
        raise RequestError(str(last_error))

    def post_json(self, url: str, payload: dict) -> HttpResponse:
        """POST a JSON body with the same throttling and retry rules as get()."""
        throttle = self._throttle(url)
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            throttle.wait()
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    self._backoff(attempt)
                    continue
                raise RequestError(str(exc)) from exc
            status = resp.status_code
            if status in self.RETRYABLE and attempt < self.max_attempts:
                self._backoff(attempt)
                continue
            return HttpResponse(status, _decode(resp), resp.url)
        raise RequestError(str(last_error))

    def _backoff(self, attempt: int):
        import random

        self.sleep(self.backoff_base**attempt + random.random() * 0.5)


def stream_to(client: HttpClient, url: str, dest: Path) -> int:
    """Stream a URL to disk as bytes; returns the HTTP status."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.session.get(url, timeout=client.timeout, stream=True) as resp:
        if resp.status_code != 200:
            return resp.status_code
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return 200
