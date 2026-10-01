from django.conf import settings
from rest_framework import serializers

from ..crypto import decrypt, mask
from .base import Connector


class AIProvider(Connector):
    """An LLM endpoint a workspace provides for AI features (the concierge, for now)."""

    has_secret = True

    def base_url(self, integration) -> str:
        raise NotImplementedError

    def api_key(self, integration) -> str:
        return decrypt(integration.credentials.get("api_key", ""))

    def clean_common(self, config):
        model = (config.get("model") or "").strip()
        if not model:
            raise serializers.ValidationError({"model": "Name the model, e.g. meta-llama/llama-3.3-70b-instruct."})
        return {"model": model[:200], "exclusive": bool(config.get("exclusive", False))}

    def info(self, integration):
        key = self.api_key(integration)
        return {"base_url": self.base_url(integration), "api_key": mask(key) if key else None}

    def test(self, integration) -> str:
        from concierge.llm import Provider, chat

        res = chat(Provider.from_integration(integration), [{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=10)
        return f"{integration.config['model']} replied: {(res['message'].get('content') or '').strip()[:60]}"


class OpenRouter(AIProvider):
    kind = "openrouter"

    def base_url(self, integration):
        return settings.OPENROUTER_BASE_URL

    def clean_config(self, config, integration):
        return self.clean_common(config)


class OpenAICompatible(AIProvider):
    kind = "openai_compatible"

    def base_url(self, integration):
        return integration.config.get("base_url", "")

    def clean_config(self, config, integration):
        base = (config.get("base_url") or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise serializers.ValidationError({"base_url": "Enter the API base URL, e.g. https://llm.example.com/v1."})
        return {**self.clean_common({"exclusive": True, **config}), "base_url": base}
