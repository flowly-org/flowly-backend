"""Staff-only admin API: runtime settings, service checks, users & plans, overview."""

import secrets
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, OuterRef, Q, Sum
from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import conf

User = get_user_model()


class StaffView(APIView):
    permission_classes = [permissions.IsAdminUser]


class SettingsView(StaffView):
    def get(self, request):
        return Response([conf.describe(s) for s in conf.SPECS])

    def patch(self, request):
        """{KEY: value} sets an override, {KEY: null} reverts to the environment value."""
        unknown = [k for k in request.data if k not in conf.SPEC_BY_KEY]
        if unknown:
            raise ValidationError({"detail": f"Unknown settings: {', '.join(unknown)}"})
        old_token = conf.get("TELEGRAM_BOT_TOKEN")
        errors = {}
        for key, value in request.data.items():
            try:
                conf.set_value(key, value, user=request.user)
            except ValueError as exc:
                errors[key] = str(exc)
        if errors:
            raise ValidationError(errors)
        if "TELEGRAM_BOT_TOKEN" in request.data and conf.get("TELEGRAM_BOT_TOKEN") != old_token:
            self._telegram_token_changed()
        return self.get(request)

    @staticmethod
    def _telegram_token_changed():
        from concierge import telegram
        from concierge.models import BotState

        BotState.objects.filter(key="telegram_offset").delete()
        if not conf.get("TELEGRAM_BOT_TOKEN"):
            return
        try:
            if conf.get("TELEGRAM_MODE") == "webhook":
                enable_webhook()
            else:
                telegram.delete_webhook()  # a leftover webhook would block polling
        except Exception:
            pass


def enable_webhook():
    from concierge import telegram

    base = conf.get("API_PUBLIC_URL")
    if not base.startswith("https://"):
        raise ValidationError({"detail": "Set an https API URL first (General → API URL)."})
    secret = conf.get("TELEGRAM_WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    conf.set_value("TELEGRAM_WEBHOOK_SECRET", secret)
    telegram.set_webhook(f"{base}/api/concierge/telegram/webhook/", secret)
    conf.set_value("TELEGRAM_MODE", "webhook")


class TelegramWebhookView(StaffView):
    def post(self, request):
        from concierge import telegram

        if not telegram.enabled():
            raise ValidationError({"detail": "Set the bot token first."})
        if request.data.get("enable"):
            enable_webhook()
        else:
            telegram.delete_webhook()
            conf.set_value("TELEGRAM_MODE", "polling")
        info = telegram.call("getWebhookInfo") or {}
        return Response({"mode": conf.get("TELEGRAM_MODE"), "webhook": info.get("url", ""), "pending": info.get("pending_update_count", 0)})


class CheckView(StaffView):
    """POST /api/admin/check/<service>/ — try the service with the current settings."""

    def post(self, request, service):
        try:
            message = getattr(self, f"check_{service}")()
            return Response({"ok": True, "message": message})
        except AttributeError:
            return Response({"detail": "Unknown service."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as exc:  # surface any failure to the admin
            body = getattr(exc, "body", "")
            return Response({"ok": False, "message": f"{exc} {body[:300] if body else ''}".strip()})

    def check_telegram(self):
        from concierge import telegram

        if not telegram.enabled():
            raise RuntimeError("No bot token set.")
        me = telegram.call("getMe") or {}
        info = telegram.call("getWebhookInfo") or {}
        mode = conf.get("TELEGRAM_MODE")
        hook = info.get("url") or "none"
        return f"Bot @{me.get('username')} is reachable. Mode: {mode}; webhook: {hook}."

    def check_openrouter(self):
        from concierge.llm import Provider, chat

        provider = Provider.platform_default()
        if provider is None:
            raise RuntimeError("No OpenRouter API key set.")
        res = chat(provider, [{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=10)
        return f"{provider.model} replied: {(res['message'].get('content') or '').strip()[:60]}"

    def check_s3(self):
        from django.core.files.base import ContentFile

        from tasks.models import AttachmentStorage, uploads_enabled

        if not uploads_enabled():
            raise RuntimeError("No bucket configured.")
        storage = AttachmentStorage()
        name = storage.save(f"attachments/_healthcheck/{uuid.uuid4().hex}.txt", ContentFile(b"flowly"))
        try:
            url = storage.url(name)
        finally:
            storage.delete(name)
        return f"Wrote and deleted a test file; signed URLs look like {url.split('?')[0]}"

    def check_github(self):
        from integrations import github

        app = github.get_app()
        if app is None:
            raise RuntimeError("The GitHub App hasn't been created yet (Integrations → GitHub).")
        data = github.api("GET", "/app", app_auth=True)
        installs = github.api("GET", "/app/installations", app_auth=True) or []
        return f"App {data.get('slug')} OK; {len(installs)} installation(s)."


class AdminUserSerializer(serializers.ModelSerializer):
    workspaces = serializers.IntegerField(read_only=True)
    tokens_30d = serializers.IntegerField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "plan", "plan_expires_at", "is_staff", "is_active",
                  "date_joined", "last_login", "workspaces", "tokens_30d"]
        read_only_fields = ["id", "email", "full_name", "date_joined", "last_login"]


class UsersView(StaffView):
    def queryset(self):
        since = timezone.localdate() - timedelta(days=30)
        from concierge.models import AIUsage

        tokens = (
            AIUsage.objects.filter(user=OuterRef("pk"), date__gte=since)
            .values("user")
            .annotate(t=Sum("prompt_tokens") + Sum("completion_tokens"))
            .values("t")
        )
        from django.db.models import IntegerField, Subquery
        from django.db.models.functions import Coalesce

        return User.objects.annotate(
            workspaces=Count("memberships", distinct=True),
            tokens_30d=Coalesce(Subquery(tokens, output_field=IntegerField()), 0),
        ).order_by("-date_joined")

    def get(self, request):
        qs = self.queryset()
        q = request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(email__icontains=q) | Q(full_name__icontains=q))
        return Response(AdminUserSerializer(qs[:200], many=True).data)


class UserDetailView(UsersView):
    def patch(self, request, pk):
        user = self.queryset().filter(pk=pk).first()
        if user is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if user.pk == request.user.pk and (request.data.get("is_staff") is False or request.data.get("is_active") is False):
            raise ValidationError({"detail": "You can't remove your own admin access."})
        serializer = AdminUserSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(AdminUserSerializer(self.queryset().get(pk=pk)).data)


class OverviewView(StaffView):
    def get(self, request):
        from concierge.models import AIUsage, ConciergeProfile, Notification
        from integrations.models import Delivery, Integration
        from tasks.models import Task
        from workspaces.models import Workspace

        today = timezone.localdate()
        usage = AIUsage.objects.filter(date=today).aggregate(t=Sum("prompt_tokens"), c=Sum("completion_tokens"), r=Sum("requests"))
        return Response(
            {
                "users": User.objects.count(),
                "pro_users": User.objects.filter(plan="pro").count(),
                "workspaces": Workspace.objects.count(),
                "tasks": Task.objects.count(),
                "integrations": Integration.objects.filter(enabled=True).count(),
                "failed_deliveries_24h": Delivery.objects.filter(status="failed", created_at__gte=timezone.now() - timedelta(days=1)).count(),
                "pending_deliveries": Delivery.objects.filter(status="pending", direction="out").count(),
                "telegram_linked": ConciergeProfile.objects.filter(telegram_chat_id__isnull=False).count(),
                "telegram_pending": Notification.objects.filter(telegram="pending").count(),
                "ai_today": {"requests": usage["r"] or 0, "tokens": (usage["t"] or 0) + (usage["c"] or 0)},
            }
        )
