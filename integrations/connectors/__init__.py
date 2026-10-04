from .ai import OpenAICompatible, OpenRouter
from .donetick import Donetick
from .email import Email
from .github import GitHub
from .slack import Slack
from .teamchat import SlackBot, TelegramGroup
from .webhooks import IncomingWebhook, OutgoingWebhook
from .wiki import Wiki

REGISTRY = {
    c.kind: c
    for c in (GitHub(), Slack(), OutgoingWebhook(), IncomingWebhook(), Email(), OpenRouter(), OpenAICompatible(),
              TelegramGroup(), SlackBot(), Wiki(), Donetick())
}
AI_KINDS = ["openrouter", "openai_compatible"]
