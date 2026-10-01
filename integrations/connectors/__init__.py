from .email import Email
from .github import GitHub
from .slack import Slack
from .webhooks import IncomingWebhook, OutgoingWebhook

REGISTRY = {c.kind: c for c in (GitHub(), Slack(), OutgoingWebhook(), IncomingWebhook(), Email())}
