"""Model pricing display for /models.

Most providers don't expose prices via their APIs. OpenRouter is the notable
exception: ``GET /api/v1/models`` returns per-model ``pricing.prompt`` /
``pricing.completion`` in USD per *token*. Since OpenRouter is configured as
an openai-compatible provider pointing at that base URL, we opportunistically
probe any openai-compatible endpoint for that response shape; anything that
doesn't answer with pricing data is simply displayed without prices.
"""

from __future__ import annotations

import httpx

_TIMEOUT = 6.0


def fetch_prices(base_url: str, api_key: str | None) -> dict[str, tuple[float, float]]:
    """Return {model_id: (prompt_usd_per_token, completion_usd_per_token)}.

    Best-effort: any network/HTTP/shape problem yields an empty dict, so the
    caller can treat pricing as purely optional decoration.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(url, headers=headers)
        if r.status_code >= 400:
            return {}
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return {}
    prices: dict[str, tuple[float, float]] = {}
    for m in data.get("data", []):
        if not isinstance(m, dict):
            continue
        p = m.get("pricing")
        if not m.get("id") or not isinstance(p, dict):
            continue
        try:
            prompt = float(p.get("prompt") or 0)
            completion = float(p.get("completion") or 0)
        except (TypeError, ValueError):
            continue
        prices[m["id"]] = (prompt, completion)
    return prices


def format_price(price: tuple[float, float]) -> str:
    """Format (prompt, completion) per-token USD as a compact per-1M-token string."""
    prompt, completion = price
    if prompt == 0 and completion == 0:
        return "free"
    return f"${prompt * 1e6:g}/${completion * 1e6:g} per 1M tok"
