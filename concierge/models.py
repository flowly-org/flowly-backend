import secrets
from datetime import time

from django.conf import settings
from django.db import models


class ConciergeProfile(models.Model):
    """A user's personal assistant settings (Pro feature)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="concierge")
    enabled = models.BooleanField(default=True)
    #: What the assistant calls itself.
    name = models.CharField(max_length=40, default="Flo")
    #: "auto" = answer in the language the user writes in; otherwise a code from concierge.i18n.LANGUAGES.
    language = models.CharField(max_length=10, default="auto")
    timezone = models.CharField(max_length=64, default="Europe/Rome")
    brief_time = models.TimeField(default=time(8, 0), help_text="Local time of the morning brief.")
    morning_brief = models.BooleanField(default=True)
    alerts = models.BooleanField(default=True, help_text="Send urgent changes to Telegram.")
    last_brief_on = models.DateField(null=True, blank=True)
    #: Weekly review: "fri", "sun" or "off", at review_time (local).
    review_day = models.CharField(max_length=3, default="fri")
    review_time = models.TimeField(default=time(16, 0))
    #: How it talks: a key of concierge.personality.PERSONALITIES.
    personality = models.CharField(max_length=20, default="friendly")
    #: Long-term notes the assistant keeps about the user ("remember that…", and what it learns).
    memory = models.TextField(blank=True)
    #: Let it distil lasting preferences from conversations into `memory` (once a day).
    learn = models.BooleanField(default=True)
    memory_updated_at = models.DateTimeField(null=True, blank=True)
    #: Workspace AI provider to use instead of the platform key (must be in a workspace the user belongs to).
    provider = models.ForeignKey(
        "integrations.Integration", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    telegram_chat_id = models.BigIntegerField(null=True, blank=True, unique=True)
    telegram_username = models.CharField(max_length=64, blank=True)
    link_code = models.CharField(max_length=40, blank=True, db_index=True)
    link_expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def new_link_code(self):
        self.link_code = secrets.token_urlsafe(16)
        return self.link_code


class ConciergeMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="concierge_messages")
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField()
    channel = models.CharField(max_length=10, default="telegram")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class Notification(models.Model):
    """In-app notification; Pro users with Telegram linked also get it from the concierge."""

    class Kind(models.TextChoices):
        ASSIGNED = "assigned", "Assigned to you"
        URGENT = "urgent", "Urgent task for today"
        COMMENT = "comment", "New comment"
        BRIEF = "brief", "Daily brief"
        SHARED = "shared", "Shared with you"
        FOCUS = "focus", "Focus nudge"
        REMINDER = "reminder", "Reminder"

    class Telegram(models.TextChoices):
        NONE = "none", "Not sent"
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    workspace = models.ForeignKey("workspaces.Workspace", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    task = models.ForeignKey("tasks.Task", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    telegram = models.CharField(max_length=10, choices=Telegram.choices, default=Telegram.NONE)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "read_at"]), models.Index(fields=["telegram"])]


class AIUsage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    date = models.DateField()
    provider = models.CharField(max_length=100)
    requests = models.PositiveIntegerField(default=0)
    prompt_tokens = models.PositiveIntegerField(default=0)
    completion_tokens = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "date", "provider"], name="unique_ai_usage")]


class BotState(models.Model):
    """Tiny key/value store for bot bookkeeping (e.g. the Telegram polling offset)."""

    key = models.CharField(max_length=50, unique=True)
    value = models.CharField(max_length=200)


class TeamMessage(models.Model):
    """Recent conversation of a team chat (Telegram group, Slack), so follow-up questions have context."""

    integration = models.ForeignKey("integrations.Integration", on_delete=models.CASCADE, related_name="team_messages")
    role = models.CharField(max_length=10)
    author = models.CharField(max_length=120, blank=True)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class BotJob(models.Model):
    """Work a chat platform handed us that must be answered later (Slack wants a reply within 3 seconds)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=30)
    integration = models.ForeignKey("integrations.Integration", null=True, on_delete=models.CASCADE, related_name="+")
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class FocusNudge(models.Model):
    """ADHD mode: a check-in, break nudge or heads-up already sent (so each goes out once)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="unique_focus_nudge")]
