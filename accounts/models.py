from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """Email-based user; `username` is dropped."""

    username = None
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    class Plan(models.TextChoices):
        FREE = "free", "Free"
        PRO = "pro", "Pro"

    plan = models.CharField(max_length=10, choices=Plan.choices, default=Plan.FREE)
    plan_expires_at = models.DateTimeField(null=True, blank=True, help_text="Empty = no expiry.")
    #: "stripe" while a Stripe subscription drives the plan; "manual" when an admin set it.
    plan_source = models.CharField(max_length=10, default="manual")
    #: UI language (en, it, es, fr, de, pt); also used for default names in new workspaces.
    language = models.CharField(max_length=5, default="en")
    #: Service identity behind a workspace MCP token (not a person; can't sign in).
    is_bot = models.BooleanField(default=False)
    #: Where the user lives, e.g. "Bologna, Italy" (several separated by ";"). Calendar events elsewhere need preparation.
    home_location = models.CharField(max_length=200, blank=True)
    github_login = models.CharField(
        max_length=39, blank=True, help_text="GitHub username, used to match issue assignees."
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    @property
    def has_pro(self):
        from django.utils import timezone

        if self.is_staff:
            return True
        return self.plan == self.Plan.PRO and (self.plan_expires_at is None or self.plan_expires_at > timezone.now())

    def __str__(self):
        return self.full_name or self.email


class ApiToken(models.Model):
    """Personal access token for the API and the MCP server. Only a hash is stored."""

    PREFIX = "flw_"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=80)
    #: First characters of the token, to recognise it in the list.
    hint = models.CharField(max_length=16)
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @staticmethod
    def hash(raw: str) -> str:
        import hashlib

        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, user, name):
        import secrets

        raw = cls.PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(user=user, name=name, hint=raw[:10], token_hash=cls.hash(raw))
        return token, raw
