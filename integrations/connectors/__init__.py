from .ai import OpenAICompatible, OpenRouter
from .discord import Discord
from .donetick import Donetick
from .email import Email
from .github import GitHub
from .slack import Slack
from .stairway import Stairway
from .teamchat import SlackBot, TelegramGroup
from .webhooks import IncomingWebhook, OutgoingWebhook
from .wiki import Wiki

REGISTRY = {
    c.kind: c
    for c in (GitHub(), Slack(), Discord(), OutgoingWebhook(), IncomingWebhook(), Email(), OpenRouter(), OpenAICompatible(),
              TelegramGroup(), SlackBot(), Wiki(), Donetick(), Stairway())
}
AI_KINDS = ["openrouter", "openai_compatible"]
