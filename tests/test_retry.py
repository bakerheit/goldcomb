"""Unit tests for the shared retry helper in goldcomb.providers.base.

Covers retry_call (attempt counting, backoff schedule, jitter, exhaustion) and
the is_retryable/error_status classification (network errors, 429, 5xx vs.
non-retryable 4xx). All fakes — no real network, and sleep is injected so the
tests never actually wait.
"""

import httpx
import pytest

from goldcomb.providers.base import (
    ProviderError,
    _backoff_delay,
    error_status,
    is_retryable,
    retry_call,
)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError without any network I/O."""
    req = httpx.Request("POST", "https://api.example.com/v1/messages")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


class Flaky:
    """Callable that raises each of ``errors`` in turn, then returns ``result``."""

    def __init__(self, errors, result="ok"):
        self.errors = list(errors)
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


# ---- retry_call -------------------------------------------------------------


def test_succeeds_first_try_no_sleep():
    sleeps = []
    flaky = Flaky([], result="done")
    assert retry_call(flaky, sleep=sleeps.append) == "done"
    assert flaky.calls == 1
    assert sleeps == []


def test_retries_transient_until_success():
    sleeps = []
    err = ProviderError("HTTP 503: overloaded")
    flaky = Flaky([err, err], result="recovered")
    out = retry_call(flaky, base_delay=1.0, jitter=0.0, sleep=sleeps.append)
    assert out == "recovered"
    assert flaky.calls == 3
    assert sleeps == [1.0, 2.0]  # exponential: base * 2**attempt


def test_does_not_retry_client_error_4xx():
    flaky = Flaky([ProviderError("HTTP 400: invalid request")])
    with pytest.raises(ProviderError, match="HTTP 400"):
        retry_call(flaky, sleep=lambda s: None)
    assert flaky.calls == 1


@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_other_4xx_never_retried(status):
    flaky = Flaky([ProviderError(f"HTTP {status}: nope")])
    with pytest.raises(ProviderError):
        retry_call(flaky, sleep=lambda s: None)
    assert flaky.calls == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses_retried(status):
    flaky = Flaky([ProviderError(f"HTTP {status}: transient")])
    assert retry_call(flaky, sleep=lambda s: None) == "ok"
    assert flaky.calls == 2


def test_network_error_retried():
    req = httpx.Request("POST", "https://api.example.com/v1/messages")
    flaky = Flaky([httpx.ConnectError("connection refused", request=req)])
    assert retry_call(flaky, sleep=lambda s: None) == "ok"
    assert flaky.calls == 2


def test_unrecognized_exception_not_retried():
    flaky = Flaky([ValueError("bug in our code")])
    with pytest.raises(ValueError):
        retry_call(flaky, sleep=lambda s: None)
    assert flaky.calls == 1


def test_exhaustion_reraises_last_exception():
    sleeps = []
    errs = [ProviderError(f"HTTP 500: boom {i}") for i in range(3)]
    flaky = Flaky(errs)
    with pytest.raises(ProviderError, match="boom 2"):
        retry_call(flaky, max_attempts=3, jitter=0.0, sleep=sleeps.append)
    assert flaky.calls == 3
    assert len(sleeps) == 2


def test_max_attempts_one_means_no_retry():
    flaky = Flaky([ProviderError("HTTP 503: overloaded")])
    with pytest.raises(ProviderError):
        retry_call(flaky, max_attempts=1, sleep=lambda s: None)
    assert flaky.calls == 1


def test_max_attempts_must_be_positive():
    with pytest.raises(ValueError):
        retry_call(lambda: None, max_attempts=0, sleep=lambda s: None)


def test_max_delay_caps_backoff():
    sleeps = []
    err = ProviderError("HTTP 429: slow down")
    flaky = Flaky([err] * 4)  # 4 failures -> 4 sleeps before the 5th succeeds
    assert retry_call(
        flaky, max_attempts=5, base_delay=1.0, max_delay=3.0,
        jitter=0.0, sleep=sleeps.append,
    ) == "ok"
    assert sleeps == [1.0, 2.0, 3.0, 3.0]


def test_jitter_stays_within_bounds():
    for attempt in range(6):
        for _ in range(50):
            delay = _backoff_delay(attempt, 1.0, 30.0, 0.5)
            base = min(30.0, 1.0 * 2**attempt)
            assert base <= delay <= base * 1.5


def test_custom_is_retryable_fn():
    # Force-retriable classifier: even a bug gets retried.
    flaky = Flaky([ValueError("treated as transient")])
    out = retry_call(flaky, is_retryable_fn=lambda e: True, sleep=lambda s: None)
    assert out == "ok"
    assert flaky.calls == 2


def test_httpx_status_error_retried_by_response_status():
    flaky = Flaky([_http_status_error(429), _http_status_error(500)])
    assert retry_call(flaky, jitter=0.0, sleep=lambda s: None) == "ok"
    assert flaky.calls == 3


def test_httpx_status_error_4xx_not_retried():
    flaky = Flaky([_http_status_error(404)])
    with pytest.raises(httpx.HTTPStatusError):
        retry_call(flaky, sleep=lambda s: None)
    assert flaky.calls == 1


# ---- is_retryable / error_status --------------------------------------------


def test_is_retryable_network_error():
    req = httpx.Request("GET", "https://api.example.com/models")
    assert is_retryable(httpx.ConnectTimeout("timed out", request=req)) is True


def test_error_status_from_provider_error_message():
    assert error_status(ProviderError("HTTP 503: overloaded")) == 503
    assert error_status(ProviderError("Network error talking to x: refused")) is None
    assert error_status(RuntimeError("plain")) is None


def test_error_status_from_attribute():
    class Coded(Exception):
        status_code = 502

    assert error_status(Coded("bad gateway")) == 502


def test_error_status_from_httpx_exception():
    assert error_status(_http_status_error(429)) == 429
