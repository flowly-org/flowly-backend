"""Minimal OpenAI-compatible chat client (OpenRouter, local servers, any /v1/chat/completions)."""

from dataclasses import dataclass

from django.db.models import F
from django.utils import timezone

from integrations.http import DeliveryError, send

from instance import conf


class LLMUnavailable(Exception):
    pass


@dataclass
class Provider:
    base_url: str
    api_key: str
    model: str
    label: str
    platform: bool = False
    integration_id: int | None = None
    workspace_id: int | None = None

    @classmethod
    def platform_default(cls):
        if not conf.get("OPENROUTER_API_KEY"):
            return None
        return cls(conf.get("OPENROUTER_BASE_URL"), conf.get("OPENROUTER_API_KEY"), conf.get("OPENROUTER_MODEL"), "Flowly", platform=True)

    @classmethod
    def from_integration(cls, integration):
        from integrations.connectors import REGISTRY

        connector = REGISTRY[integration.kind]
        return cls(
            base_url=connector.base_url(integration).rstrip("/"),
            api_key=connector.api_key(integration),
            model=integration.config["model"],
            label=f"{integration.workspace.name}: {integration.name}",
            integration_id=integration.id,
            workspace_id=integration.workspace_id,
        )


def chat(provider: Provider, messages, tools=None, max_tokens=700, user=None) -> dict:
    """One chat completion. Returns {"message": {...}, "usage": {...}}; raises DeliveryError."""
    if user is not None and provider.platform:
        used = sum(
            u.prompt_tokens + u.completion_tokens
            for u in user_usage(user)
        )
        if used >= conf.get("CONCIERGE_DAILY_TOKENS"):
            raise LLMUnavailable("You've reached today's AI usage limit. It resets tomorrow.")
    body = {"model": provider.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    if provider.platform:
        headers.update({"HTTP-Referer": conf.get("PUBLIC_URL") or "https://flowly.net", "X-Title": "Flowly"})
    res = send(
        "POST", f"{provider.base_url}/chat/completions", json=body, headers=headers,
        timeout=60, check_url=not provider.platform,
    ).json()
    if "choices" not in res or not res["choices"]:
        raise DeliveryError(f"Unexpected LLM response: {str(res)[:300]}", retry=False)
    usage = res.get("usage") or {}
    if user is not None:
        record_usage(user, provider, usage)
    return {"message": res["choices"][0]["message"], "usage": usage}


def user_usage(user):
    from .models import AIUsage

    return AIUsage.objects.filter(user=user, date=timezone.localdate(), provider="Flowly")


def record_usage(user, provider: Provider, usage: dict):
    from .models import AIUsage

    row, _ = AIUsage.objects.get_or_create(user=user, date=timezone.localdate(), provider=provider.label[:100])
    AIUsage.objects.filter(pk=row.pk).update(
        requests=F("requests") + 1,
        prompt_tokens=F("prompt_tokens") + int(usage.get("prompt_tokens") or 0),
        completion_tokens=F("completion_tokens") + int(usage.get("completion_tokens") or 0),
    )
