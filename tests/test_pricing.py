"""Tests for model pricing display (goldcomb/pricing.py)."""

from goldcomb import pricing
from goldcomb.pricing import fetch_prices, format_price


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _mock_client(monkeypatch, payload, status=200):
    class FakeClient:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            return _Resp(payload, status)

    monkeypatch.setattr(pricing.httpx, "Client", FakeClient)


def test_parses_openrouter_pricing(monkeypatch):
    _mock_client(monkeypatch, {
        "data": [
            {"id": "openai/gpt-4o", "pricing": {"prompt": "0.0000025", "completion": "0.00001"}},
            {"id": "free/model", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "no-pricing-model"},
            {"pricing": {"prompt": "1"}},  # no id — skipped
        ]
    })
    prices = fetch_prices("https://openrouter.ai/api/v1", "key")
    assert prices["openai/gpt-4o"] == (0.0000025, 0.00001)
    assert prices["free/model"] == (0.0, 0.0)
    assert "no-pricing-model" not in prices
    assert len(prices) == 2


def test_endpoint_without_pricing_yields_empty(monkeypatch):
    # e.g. Ollama / vLLM: plain OpenAI-style {data: [{id: ...}]}
    _mock_client(monkeypatch, {"data": [{"id": "llama3"}, {"id": "qwen2"}]})
    assert fetch_prices("http://localhost:11434/v1", None) == {}


def test_http_error_and_bad_json_yield_empty(monkeypatch):
    _mock_client(monkeypatch, {}, status=401)
    assert fetch_prices("https://x/v1", "bad-key") == {}
    _mock_client(monkeypatch, ValueError("not json"))
    assert fetch_prices("https://x/v1", None) == {}


def test_bad_price_values_are_skipped(monkeypatch):
    _mock_client(monkeypatch, {
        "data": [
            {"id": "ok", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
            {"id": "junk", "pricing": {"prompt": "n/a", "completion": None}},
        ]
    })
    prices = fetch_prices("https://openrouter.ai/api/v1", "k")
    assert list(prices) == ["ok"]


def test_format_price():
    assert format_price((0.0000025, 0.00001)) == "$2.5/$10 per 1M tok"
    assert format_price((0.0, 0.0)) == "free"
    assert format_price((0.00000015, 0.0000006)) == "$0.15/$0.6 per 1M tok"


def test_format_price_round_trips_fetch(monkeypatch):
    _mock_client(monkeypatch, {
        "data": [{"id": "m", "pricing": {"prompt": "0.000003", "completion": "0.000015"}}]
    })
    prices = fetch_prices("https://openrouter.ai/api/v1", "k")
    assert format_price(prices["m"]) == "$3/$15 per 1M tok"
