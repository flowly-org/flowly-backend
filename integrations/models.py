import secrets

from django.conf import settings
from django.db import models

from tasks.models import Project, Task
from workspaces.models import Workspace


def new_token():
    # Lowercase so it survives use as an email local part.
    return secrets.token_hex(12)


def new_secret():
    return secrets.token_urlsafe(32)


class Integration(models.Model):
    """A connector or webhook attached to a workspace, optionally narrowed to one project.

    Kind-specific settings live in `config` (validated by the connector, see connectors/).
    """

    class Kind(models.TextChoices):
        GITHUB = "github", "GitHub"
        SLACK = "slack", "Slack"
        OUTGOING_WEBHOOK = "outgoing_webhook", "Outgoing webhook"
        INCOMING_WEBHOOK = "incoming_webhook", "Incoming webhook"
        EMAIL = "email", "Email in"
        OPENROUTER = "openrouter", "OpenRouter"
        OPENAI_COMPATIBLE = "openai_compatible", "OpenAI-compatible endpoint"
        TELEGRAM_GROUP = "telegram_group", "Telegram group"
        SLACK_BOT = "slack_bot", "Slack app"
        WIKI = "wiki", "Wiki"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="integrations")
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.CASCADE, related_name="integrations",
        help_text="Limit to one project; empty = whole workspace.",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    name = models.CharField(max_length=120)
    enabled = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)
    #: Encrypted secrets (e.g. API keys); never returned by the API.
    credentials = models.JSONField(default=dict, blank=True)
    token = models.CharField(max_length=40, unique=True, default=new_token, help_text="Public id used in incoming URLs and email addresses.")
    secret = models.CharField(max_length=64, default=new_secret, help_text="Signs outgoing payloads / verifies incoming ones.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.name}"

    def covers(self, task_or_project_id):
        """Whether a task (or a task in this project) is in this integration's scope."""
        project_id = getattr(task_or_project_id, "project_id", task_or_project_id)
        return self.project_id is None or self.project_id == project_id


class ExternalLink(models.Model):
    """Ties a task to its counterpart elsewhere (e.g. a GitHub issue) for one integration."""

    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name="links")
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="external_links")
    external_id = models.CharField(max_length=255)
    url = models.URLField(max_length=500, blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["integration", "external_id"], name="unique_external_link"),
        ]


class Delivery(models.Model):
    """One incoming or outgoing call. Outgoing ones double as the job queue for the worker."""

    class Direction(models.TextChoices):
        IN = "in", "Incoming"
        OUT = "out", "Outgoing"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        IGNORED = "ignored", "Ignored"

    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name="deliveries")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    event = models.CharField(max_length=60)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class GitHubApp(models.Model):
    """Credentials of this instance's GitHub App (created through the manifest flow). Singleton."""

    app_id = models.BigIntegerField()
    slug = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    owner = models.CharField(max_length=100, blank=True)
    html_url = models.URLField()
    client_id = models.CharField(max_length=100)
    client_secret = models.CharField(max_length=200)
    webhook_secret = models.CharField(max_length=200)
    private_key = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.slug


class EmailAlias(models.Model):
    """A readable address for an Email-in integration: <name>@<inbound domain> (besides its token address)."""

    integration = models.ForeignKey(Integration, on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField(max_length=40, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
