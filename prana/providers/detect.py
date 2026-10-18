"""Provider auto-detection from API key prefix.

Sniffing order (most specific first):
  sk-ant-… → anthropic · AIza… → gemini · sk-… → openai

Unknown prefix → ProviderError with a clear message. There is deliberately
NO silent fallback to a default provider: guessing wrong would send the
user's key to the wrong vendor, which is a secrets-handling failure.
An explicit `--provider` override always wins over sniffing.
"""

from __future__ import annotations

from typing import Optional, Type

from .anthropic import AnthropicProvider
from .base import Provider, ProviderError
from .gemini import GeminiProvider
from .openai import OpenAIProvider

PROVIDERS: dict[str, Type[Provider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


def detect_provider(api_key: str) -> str:
    """Return the provider name for a key, or raise ProviderError."""
    key = (api_key or "").strip()
    if not key:
        raise ProviderError(
            "no API key provided — pass --key, set OPENAI_API_KEY / "
            "ANTHROPIC_API_KEY / GEMINI_API_KEY, or run `prana --save-key`"
        )
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith("sk-"):
        return "openai"
    # Show only a safe prefix in the error — never echo the key.
    hint = (key[:4] + "…") if len(key) > 4 else "?"
    raise ProviderError(
        f"could not detect provider from key starting with '{hint}' — "
        f"pass --provider explicitly (openai|anthropic|gemini)"
    )


def resolve_provider(api_key: str, provider: Optional[str] = None) -> Type[Provider]:
    """Pick the provider class: explicit override wins, else sniff the key."""
    if provider:
        name = provider.strip().lower()
        if name not in PROVIDERS:
            raise ProviderError(
                f"unknown provider '{provider}' — choose from: {', '.join(sorted(PROVIDERS))}"
            )
        return PROVIDERS[name]
    return PROVIDERS[detect_provider(api_key)]
