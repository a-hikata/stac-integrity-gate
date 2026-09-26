"""Polite-crawling primitives for collection audits (stdlib only).

* :class:`RateLimiter` — thread-safe token bucket shared by STAC JSON requests
  and raster header opens.
* :class:`JsonClient` — STAC JSON fetcher with bounded retries for transient
  failures only (timeouts, connection resets, HTTP 429 honouring
  ``Retry-After``, HTTP 5xx). HTTP 202 with an empty body (a WAF / bot
  challenge page) and every other 4xx are *not* retried.
* :class:`HeaderCache` — per-run, thread-safe, single-flight cache of raster
  header snapshots keyed by resolved href, with a bound on concurrent opens.

Everything here is operational. Failures raise :class:`OperationalFetchError`
(or are cached as open failures) and must never be turned into semantic
ERROR findings.
"""

from __future__ import annotations

import email.utils
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .audit import USER_AGENT

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class OperationalFetchError(RuntimeError):
    """A STAC JSON document could not be fetched (network/HTTP/WAF)."""

    def __init__(self, url: str, reason: str, *, status: int | None = None, attempts: int = 1):
        super().__init__(f"{reason} ({url}; attempts={attempts})")
        self.url = url
        self.reason = reason
        self.status = status
        self.attempts = attempts

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "reason": self.reason, "status": self.status, "attempts": self.attempts}


class WafChallengeError(OperationalFetchError):
    """HTTP 202 with an empty body: the publisher is serving a bot challenge."""


class RateLimiter:
    """Thread-safe token bucket.

    ``rate`` tokens per second, bucket capacity ``burst``. ``rate=None`` or
    ``<= 0`` disables limiting. ``clock`` and ``sleep`` are injectable for
    deterministic tests.
    """

    def __init__(
        self,
        rate: float | None,
        burst: int = 1,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = rate if rate and rate > 0 else None
        self.burst = max(1, int(burst))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(self.burst)
        self._last = clock()
        self.waited = 0.0

    @classmethod
    def from_options(cls, max_rps: float | None = None, delay: float | None = None, **kw: Any) -> "RateLimiter":
        if max_rps is not None and delay is not None:
            raise ValueError("Use either max_rps or delay, not both")
        if delay is not None:
            if delay < 0:
                raise ValueError("delay must be >= 0")
            return cls(1.0 / delay if delay > 0 else None, 1, **kw)
        if max_rps is not None and max_rps < 0:
            raise ValueError("max_rps must be >= 0")
        return cls(max_rps, 1, **kw)

    def acquire(self) -> float:
        """Block until a token is available; return seconds waited."""
        if self.rate is None:
            return 0.0
        # Reserve a token under the lock (the balance may go negative), then
        # sleep outside the lock. Reservations are FIFO, so concurrent callers
        # are spaced 1/rate apart without holding the lock while sleeping.
        with self._lock:
            now = self._clock()
            self._tokens = min(float(self.burst), self._tokens + (now - self._last) * self.rate)
            self._last = now
            self._tokens -= 1.0
            wait = 0.0 if self._tokens >= 0 else -self._tokens / self.rate
            self.waited += wait
        if wait > 0:
            self._sleep(wait)
        return wait


def _retry_after_seconds(value: str | None, now: Callable[[], float] = time.time) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return max(0.0, parsed.timestamp() - now())


@dataclass
class RawResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def urllib_opener(url: str, timeout: float) -> RawResponse:
    """Single HTTP GET. HTTP error statuses are returned, not raised.

    Network-level failures (timeouts, resets, DNS) propagate as exceptions.
    Redirects (e.g. an items endpoint 302 to /search) are followed by urllib.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return RawResponse(response.status, response.read(), dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except Exception:
            pass
        return RawResponse(exc.code, body, dict(exc.headers.items()) if exc.headers else {})


def _is_transient_exception(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionResetError, ConnectionAbortedError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return isinstance(reason, (socket.timeout, TimeoutError, ConnectionResetError, ConnectionAbortedError))
    return False


class JsonClient:
    """Fetch STAC JSON with rate limiting and bounded retries."""

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        retries: int = 2,
        backoff: float = 1.0,
        backoff_max: float = 30.0,
        max_retry_after: float = 60.0,
        timeout: float = 30.0,
        opener: Callable[[str, float], RawResponse] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if retries < 0:
            raise ValueError("retries must be >= 0")
        self.limiter = limiter or RateLimiter(None)
        self.retries = retries
        self.backoff = backoff
        self.backoff_max = backoff_max
        self.max_retry_after = max_retry_after
        self.timeout = timeout
        # Looked up at construction time so tests can monkeypatch urllib_opener.
        self._opener = opener if opener is not None else urllib_opener
        self._sleep = sleep
        self._lock = threading.Lock()
        self.requests = 0
        self.retried = 0

    def _count(self, *, retry: bool = False) -> None:
        with self._lock:
            if retry:
                self.retried += 1
            else:
                self.requests += 1

    def fetch_json(self, url: str) -> dict[str, Any]:
        attempts = 0
        while True:
            attempts += 1
            self.limiter.acquire()
            self._count()
            delay: float | None = None
            try:
                resp = self._opener(url, self.timeout)
            except Exception as exc:  # network-level
                if not _is_transient_exception(exc) or attempts > self.retries:
                    raise OperationalFetchError(url, f"{type(exc).__name__}: {exc}", attempts=attempts) from exc
                delay = self._backoff(attempts)
            else:
                status = resp.status
                if status == 202 and not resp.body.strip():
                    raise WafChallengeError(
                        url,
                        "HTTP 202 with empty body (likely a WAF/bot challenge); not retried",
                        status=202,
                        attempts=attempts,
                    )
                if 200 <= status < 300:
                    try:
                        doc = json.loads(resp.body)
                    except ValueError as exc:
                        raise OperationalFetchError(url, f"invalid JSON: {exc}", status=status, attempts=attempts) from exc
                    if not isinstance(doc, dict):
                        raise OperationalFetchError(url, "JSON document is not an object", status=status, attempts=attempts)
                    return doc
                if status not in RETRYABLE_STATUS or attempts > self.retries:
                    raise OperationalFetchError(url, f"HTTP {status}", status=status, attempts=attempts)
                if status == 429 or status == 503:
                    ra = _retry_after_seconds(_header(resp.headers, "Retry-After"))
                    if ra is not None:
                        if ra > self.max_retry_after:
                            raise OperationalFetchError(
                                url,
                                f"HTTP {status} with Retry-After {ra:.0f}s exceeding cap {self.max_retry_after:.0f}s",
                                status=status,
                                attempts=attempts,
                            )
                        delay = ra
                if delay is None:
                    delay = self._backoff(attempts)
            self._count(retry=True)
            self._sleep(delay)

    def _backoff(self, attempts: int) -> float:
        return min(self.backoff_max, self.backoff * (2 ** (attempts - 1)))


def _header(headers: dict[str, str], name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return None


class _CachedFailure:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class HeaderCache:
    """Thread-safe single-flight cache of raster header snapshots.

    The same resolved href is opened at most once per run; open failures are
    cached too (re-raised to each caller, so every referencing asset still
    reports ``ASSET_UNREADABLE``). ``max_concurrent`` bounds simultaneous
    opens; each actual open also takes one token from ``limiter``.
    """

    def __init__(
        self,
        reader: Callable[[str], Any],
        *,
        max_concurrent: int | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._reader = reader
        self._limiter = limiter or RateLimiter(None)
        self._sem = threading.BoundedSemaphore(max_concurrent) if max_concurrent and max_concurrent > 0 else None
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        self._inflight: dict[str, threading.Event] = {}
        self.opens = 0
        self.hits = 0
        self.failures = 0

    def __call__(self, href: str) -> Any:
        return self.get(href)

    def get(self, href: str) -> Any:
        while True:
            with self._lock:
                if href in self._values:
                    self.hits += 1
                    value = self._values[href]
                    break
                event = self._inflight.get(href)
                if event is None:
                    event = threading.Event()
                    self._inflight[href] = event
                    owner = True
                else:
                    owner = False
            if not owner:
                event.wait()
                continue
            try:
                value = self._open(href)
            finally:
                with self._lock:
                    self._inflight.pop(href, None)
                event.set()
            break
        if isinstance(value, _CachedFailure):
            raise value.exc
        return value

    def _open(self, href: str) -> Any:
        if self._sem:
            self._sem.acquire()
        try:
            self._limiter.acquire()
            with self._lock:
                self.opens += 1
            try:
                value: Any = self._reader(href)
            except Exception as exc:
                value = _CachedFailure(exc)
                with self._lock:
                    self.failures += 1
        finally:
            if self._sem:
                self._sem.release()
        with self._lock:
            self._values[href] = value
        return value
