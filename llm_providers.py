"""
Multi-provider LLM chat completion with automatic fallback.

Anthropic (via the server's own ANTHROPIC_API_KEY) remains the default for
the offline CLI pipeline (download_event.py / build_question_bank.py). For
actions a user triggers from the web UI, the browser may additionally send
per-provider API keys the user entered in the Settings panel — those keys
live only in the browser's localStorage and arrive per-request via the
`X-LLM-Keys` header; this module never persists them to disk.

When more than one provider key is available, providers are tried in a
fixed priority order (`PROVIDER_ORDER`) and a request is retried on the next
provider only when the failure looks like "this key/provider can't be used
right now" (missing/invalid key, no permission, rate-limited or out of
credit) — not on a genuine request error, which is surfaced immediately so
it isn't masked by an unrelated fallback chain.
"""

from __future__ import annotations

import os

import requests

PROVIDER_ORDER = ["anthropic", "openai", "gemini", "deepseek", "mistral"]

# Sensible current defaults (one flagship-ish chat model per provider) and
# best-effort USD-per-million-token pricing for the cost preview shown in
# the UI before a call is made. Pricing for non-Anthropic providers is a
# ballpark estimate, not pulled live from each vendor — treat it as
# directional only. Override the model via env (mirrors the existing
# ANTHROPIC_VISION_MODEL / ANTHROPIC_DIAGRAM_MODEL convention).
PROVIDER_DEFAULTS = {
    "anthropic": {
        "model": os.environ.get("ANTHROPIC_DIAGRAM_MODEL", "claude-sonnet-4-6"),
        "price": (3.00, 15.00),
    },
    "openai": {
        "model": os.environ.get("OPENAI_CHAT_MODEL", "gpt-4.1"),
        "price": (2.00, 8.00),
    },
    "gemini": {
        "model": os.environ.get("GEMINI_CHAT_MODEL", "gemini-2.5-flash"),
        "price": (0.30, 2.50),
    },
    "deepseek": {
        "model": os.environ.get("DEEPSEEK_CHAT_MODEL", "deepseek-chat"),
        "price": (0.27, 1.10),
    },
    "mistral": {
        "model": os.environ.get("MISTRAL_CHAT_MODEL", "mistral-large-latest"),
        "price": (2.00, 6.00),
    },
}

_OPENAI_COMPATIBLE_BASE = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
}


class LLMError(Exception):
    """Raised when no provider key is configured, or every available
    provider failed."""


class _ProviderAttemptError(Exception):
    """Internal: one provider's attempt failed.

    `retryable=True` means the failure looks key/quota/rate-limit-shaped, so
    the caller should fall through to the next provider. `retryable=False`
    means it's a real error (bad request, malformed prompt, server error)
    that should be surfaced immediately instead of being hidden behind an
    unrelated fallback.
    """

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def default_keys() -> dict:
    """The CLI/server-side key set — just whatever's in the environment.
    Used when a caller doesn't have (or care about) browser-supplied keys,
    so CLI code paths behave exactly as before this module existed."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return {"anthropic": key} if key else {}


def available_providers(keys: dict | None) -> list[str]:
    """Providers in fallback order that we actually have a key for."""
    keys = keys or {}
    return [p for p in PROVIDER_ORDER if (keys.get(p) or "").strip()]


def _retryable_http(status_code: int) -> bool:
    # 401/403 (missing/invalid/under-permissioned key), 429 (rate limit),
    # 402 (some providers use this for exhausted prepaid credit) all warrant
    # falling through to the next provider rather than failing outright.
    return status_code in (401, 402, 403, 429)


def _call_anthropic(key: str, model: str, system: str | None,
                     messages: list[dict], max_tokens: int) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        kwargs["system"] = system
    try:
        resp = client.messages.create(**kwargs)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
            anthropic.RateLimitError) as e:
        raise _ProviderAttemptError(f"anthropic: {e}", retryable=True) from e
    except anthropic.APIStatusError as e:
        raise _ProviderAttemptError(f"anthropic: {e}", retryable=False) from e
    text = (resp.content[0].text or "").strip() if resp.content else ""
    usage = getattr(resp, "usage", None)
    return {
        "text": text, "model": model, "provider": "anthropic",
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "stop_reason": getattr(resp, "stop_reason", None),
    }


def _call_openai_compatible(provider: str, key: str, model: str,
                             system: str | None, messages: list[dict],
                             max_tokens: int) -> dict:
    """OpenAI, DeepSeek, and Mistral all speak the same chat-completions
    wire shape, so one HTTP call handles all three (different base URL and
    model only)."""
    chat_messages = []
    if system:
        chat_messages.append({"role": "system", "content": system})
    chat_messages.extend(messages)
    base_url = _OPENAI_COMPATIBLE_BASE[provider]
    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": chat_messages,
                  "max_tokens": max_tokens},
            timeout=120,
        )
    except requests.RequestException as e:
        raise _ProviderAttemptError(f"{provider}: request failed: {e}",
                                     retryable=True) from e
    if not r.ok:
        raise _ProviderAttemptError(
            f"{provider}: HTTP {r.status_code}: {r.text[:300]}",
            retryable=_retryable_http(r.status_code),
        )
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    usage = data.get("usage") or {}
    return {
        "text": text, "model": model, "provider": provider,
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "stop_reason": choice.get("finish_reason"),
    }


def _call_gemini(key: str, model: str, system: str | None,
                  messages: list[dict], max_tokens: int) -> dict:
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    body = {"contents": contents, "generationConfig": {"maxOutputTokens": max_tokens}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key}, json=body, timeout=120,
        )
    except requests.RequestException as e:
        raise _ProviderAttemptError(f"gemini: request failed: {e}",
                                     retryable=True) from e
    if not r.ok:
        raise _ProviderAttemptError(
            f"gemini: HTTP {r.status_code}: {r.text[:300]}",
            retryable=_retryable_http(r.status_code),
        )
    data = r.json()
    candidates = data.get("candidates") or []
    parts = (candidates[0].get("content", {}).get("parts") or []) if candidates else []
    text = "".join(p.get("text", "") for p in parts).strip()
    usage = data.get("usageMetadata") or {}
    return {
        "text": text, "model": model, "provider": "gemini",
        "input_tokens": int(usage.get("promptTokenCount", 0) or 0),
        "output_tokens": int(usage.get("candidatesTokenCount", 0) or 0),
        "stop_reason": candidates[0].get("finishReason") if candidates else None,
    }


def chat(keys: dict | None, system: str | None, messages: list[dict],
         max_tokens: int = 4096, model_overrides: dict | None = None) -> dict:
    """Run one chat-completion turn, trying each provider with a configured
    key (in PROVIDER_ORDER) until one succeeds.

    Returns {"text", "model", "provider", "input_tokens", "output_tokens",
    "stop_reason"}. Raises LLMError if no provider key was supplied, or if
    every available provider failed.
    """
    providers = available_providers(keys)
    if not providers:
        raise LLMError("No LLM API key configured. Add one in Settings.")
    model_overrides = model_overrides or {}
    errors = []
    for provider in providers:
        key = keys[provider]
        model = model_overrides.get(provider) or PROVIDER_DEFAULTS[provider]["model"]
        try:
            if provider == "anthropic":
                return _call_anthropic(key, model, system, messages, max_tokens)
            if provider == "gemini":
                return _call_gemini(key, model, system, messages, max_tokens)
            return _call_openai_compatible(provider, key, model, system,
                                            messages, max_tokens)
        except _ProviderAttemptError as e:
            errors.append(str(e))
            if not e.retryable:
                raise LLMError(str(e)) from e
            continue
    raise LLMError("All configured providers failed: " + "; ".join(errors))


def estimate_cost(keys: dict | None, system: str | None, messages: list[dict],
                   assumed_output_tokens: int = 1500,
                   model_overrides: dict | None = None) -> dict:
    """Best-effort pre-flight cost estimate for the provider `chat()` would
    actually use first (the earliest-in-PROVIDER_ORDER provider we have a
    key for).

    Anthropic gets an exact input-token count via its free count_tokens
    endpoint. Every other provider is approximated from character count
    (~4 chars/token) since there's no free, universal token-counting
    endpoint across providers — `exact` in the result tells you which case
    you got.
    """
    providers = available_providers(keys)
    if not providers:
        raise LLMError("No LLM API key configured. Add one in Settings.")
    model_overrides = model_overrides or {}
    provider = providers[0]
    model = model_overrides.get(provider) or PROVIDER_DEFAULTS[provider]["model"]
    in_price, out_price = PROVIDER_DEFAULTS[provider]["price"]
    exact = False
    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=keys[provider])
        kwargs = {"model": model, "messages": messages}
        if system:
            kwargs["system"] = system
        count = client.messages.count_tokens(**kwargs)
        input_tokens = int(getattr(count, "input_tokens", 0) or 0)
        exact = True
    else:
        chars = len(system or "") + sum(len(m.get("content", "") or "") for m in messages)
        input_tokens = max(1, chars // 4)
    cost = (input_tokens / 1_000_000 * in_price
            + assumed_output_tokens / 1_000_000 * out_price)
    return {
        "provider": provider, "model": model, "exact": exact,
        "input_tokens": input_tokens,
        "assumed_output_tokens": assumed_output_tokens,
        "estimated_cost_usd": round(cost, 4),
        "input_price_per_mtok": in_price, "output_price_per_mtok": out_price,
    }
