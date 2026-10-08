"""Staff-only admin API: runtime settings, service checks, users & plans, overview."""

import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, OuterRef, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from workspaces.models import Workspace
from rest_framework.views import APIView

from integrations.http import send

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


ACTIVE_DAYS = 7


def with_activity(qs):
    """Annotate users with what they've done: tasks created, messages to Flo, Telegram linked."""
    from django.db.models import Exists, IntegerField, Subquery
    from django.db.models.functions import Coalesce

    from concierge.models import ConciergeMessage, ConciergeProfile
    from tasks.models import Task

    def count(model, owner, **filters):
        sub = model.objects.filter(**{owner: OuterRef("pk")}, **filters).values(owner).annotate(n=Count("pk")).values("n")
        return Coalesce(Subquery(sub, output_field=IntegerField()), 0)

    return qs.annotate(
        tasks=count(Task, "created_by"),
        flo_messages=count(ConciergeMessage, "user", role="user"),
        telegram=Exists(ConciergeProfile.objects.filter(user=OuterRef("pk"), telegram_chat_id__isnull=False)),
    )


def activation_stage(user) -> str:
    """signed_up → first_task → used_flo; "active" when also seen in the last week."""
    stage = "used_flo" if user.flo_messages else "first_task" if user.tasks else "signed_up"
    recent = user.last_seen_at and user.last_seen_at > timezone.now() - timedelta(days=ACTIVE_DAYS)
    return f"{stage}" + (",active" if recent else "")


class AdminUserSerializer(serializers.ModelSerializer):
    workspaces = serializers.IntegerField(read_only=True)
    tokens_30d = serializers.IntegerField(read_only=True)
    tasks = serializers.IntegerField(read_only=True)
    flo_messages = serializers.IntegerField(read_only=True)
    telegram = serializers.BooleanField(read_only=True)
    stage = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "plan", "plan_expires_at", "plan_source", "is_staff", "is_active",
                  "date_joined", "last_login", "last_seen_at", "workspaces", "tokens_30d", "tasks", "flo_messages",
                  "telegram", "stage"]
        read_only_fields = ["id", "email", "full_name", "date_joined", "last_login", "last_seen_at", "plan_source"]

    def get_stage(self, obj):
        return activation_stage(obj)

    def update(self, instance, validated_data):
        if "plan" in validated_data or "plan_expires_at" in validated_data:
            validated_data["plan_source"] = "manual"
        return super().update(instance, validated_data)


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

        return with_activity(User.objects.annotate(
            workspaces=Count("memberships", distinct=True),
            tokens_30d=Coalesce(Subquery(tokens, output_field=IntegerField()), 0),
        )).order_by("-date_joined")

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
                "activation": activation_funnel(),
            }
        )


def activation_funnel():
    """How far real people (not bots) got: signed up, created a task, talked to Flo, linked Telegram, active this week."""
    users = list(with_activity(User.objects.filter(is_bot=False, is_active=True)))
    week = timezone.now() - timedelta(days=ACTIVE_DAYS)
    return {
        "signed_up": len(users),
        "first_task": sum(1 for u in users if u.tasks),
        "used_flo": sum(1 for u in users if u.flo_messages),
        "telegram": sum(1 for u in users if u.telegram),
        "active_7d": sum(1 for u in users if u.last_seen_at and u.last_seen_at > week),
    }


class GitHubAppAdminView(StaffView):
    """GET status · PUT {app_id, slug, client_id, client_secret?, webhook_secret?, private_key?, …} connects an
    existing GitHub App (verified with GitHub first) · DELETE disconnects it."""

    FIELDS = ("app_id", "slug", "name", "owner", "html_url", "client_id", "client_secret", "webhook_secret", "private_key")

    def urls(self):
        from mcpauth.views import api_base

        public = conf.get("PUBLIC_URL")
        return {
            "webhook_url": f"{api_base()}/api/integrations/github/webhook/",
            "callback_url": f"{public}/github/app-created",
            "setup_url": f"{public}/github/installed",
        }

    def get(self, request):
        from integrations import github
        from integrations.models import Integration

        app = github.get_app()
        data = {"configured": app is not None, **self.urls(), "app": None, "installations": [], "error": None}
        if app is None:
            return Response(data)
        data["app"] = {
            "app_id": app.app_id, "slug": app.slug, "name": app.name, "owner": app.owner, "html_url": app.html_url,
            "client_id": app.client_id, "has_client_secret": bool(app.client_secret),
            "has_webhook_secret": bool(app.webhook_secret), "has_private_key": bool(app.private_key),
            "created_at": app.created_at,
        }
        data["integrations"] = Integration.objects.filter(kind="github").count()
        try:
            for inst in github.api("GET", "/app/installations", app_auth=True) or []:
                data["installations"].append({
                    "id": inst["id"], "account": (inst.get("account") or {}).get("login"),
                    "type": (inst.get("account") or {}).get("type"), "repositories": inst.get("repository_selection"),
                    "url": inst.get("html_url"),
                })
        except Exception as exc:
            data["error"] = f"GitHub didn't accept the app credentials: {exc}"
        return Response(data)

    def put(self, request):
        from integrations import github
        from integrations.models import GitHubApp

        current = github.get_app()
        values = {f: request.data.get(f) for f in self.FIELDS if request.data.get(f) not in (None, "")}
        merged = {f: getattr(current, f) for f in self.FIELDS} if current else {}
        merged.update(values)
        missing = [f for f in ("app_id", "slug", "client_id", "webhook_secret", "private_key") if not merged.get(f)]
        if missing:
            raise ValidationError({"detail": f"Missing: {', '.join(missing)}."})
        try:
            merged["app_id"] = int(merged["app_id"])
        except (TypeError, ValueError) as exc:
            raise ValidationError({"app_id": "The App ID is a number."}) from exc
        merged["private_key"] = merged["private_key"].strip() + "\n"
        candidate = GitHubApp(**{**{f: merged.get(f) or "" for f in self.FIELDS}, "app_id": merged["app_id"]})
        if not candidate.html_url:
            candidate.html_url = f"https://github.com/apps/{candidate.slug}"
        # Verify before saving: sign an app JWT with the key and ask GitHub who we are.
        try:
            me = send("GET", f"{settings.GITHUB_API_URL}/app", check_url=False, headers={
                "Authorization": f"Bearer {github.app_jwt(candidate)}", "Accept": "application/vnd.github+json",
            }).json()
        except Exception as exc:
            raise ValidationError({"detail": f"GitHub rejected these credentials: {exc}"}) from exc
        candidate.slug = me.get("slug") or candidate.slug
        candidate.name = me.get("name") or candidate.name or candidate.slug
        candidate.owner = (me.get("owner") or {}).get("login", candidate.owner or "")
        candidate.html_url = me.get("html_url") or candidate.html_url
        GitHubApp.objects.all().delete()
        candidate.save()
        github._tokens.clear()
        return self.get(request)

    def delete(self, request):
        from integrations import github
        from integrations.models import GitHubApp

        GitHubApp.objects.all().delete()
        github._tokens.clear()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminWorkspaceSerializer(serializers.ModelSerializer):
    members = serializers.IntegerField(read_only=True)
    owner = serializers.SerializerMethodField()
    is_enterprise = serializers.BooleanField(read_only=True)
    subscription = serializers.SerializerMethodField()

    def get_subscription(self, obj):
        from payments.models import Subscription
        from payments.views import sub_data

        return sub_data(Subscription.objects.filter(customer__workspace=obj, kind="enterprise").order_by("-updated_at").first())

    def update(self, instance, validated_data):
        if "plan" in validated_data or "plan_expires_at" in validated_data:
            validated_data["plan_source"] = "manual"  # an admin's choice overrides Stripe until the next Stripe event
        return super().update(instance, validated_data)

    class Meta:
        model = Workspace
        fields = ["id", "name", "color", "plan", "plan_expires_at", "plan_source", "is_enterprise", "members", "owner",
                  "subscription", "created_at"]
        read_only_fields = ["id", "name", "color", "created_at", "plan_source"]

    def get_owner(self, obj):
        m = obj.memberships.filter(role="owner").select_related("user").first()
        return m.user.email if m else None


class WorkspacesView(StaffView):
    """Instance admins: workspaces and their plan (Enterprise unlocks team bots and own AI providers)."""

    def queryset(self):
        return Workspace.objects.annotate(members=Count("memberships", distinct=True)).order_by("name")

    def get(self, request):
        qs = self.queryset()
        q = request.query_params.get("q", "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(memberships__user__email__icontains=q)).distinct()
        return Response(AdminWorkspaceSerializer(qs[:200], many=True).data)


class WorkspaceDetailView(WorkspacesView):
    def patch(self, request, pk):
        ws = self.queryset().filter(pk=pk).first()
        if ws is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = AdminWorkspaceSerializer(ws, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(AdminWorkspaceSerializer(self.queryset().get(pk=pk)).data)


class InviteCodeSerializer(serializers.ModelSerializer):
    code = serializers.CharField(required=False, max_length=40)

    class Meta:
        from accounts.models import InviteCode

        model = InviteCode
        fields = ["id", "code", "plan", "days", "max_uses", "uses", "valid_until", "active", "note", "created_at"]
        read_only_fields = ["id", "uses", "created_at"]

    def validate_code(self, value):
        import re

        from accounts.codes import normalize
        from accounts.models import InviteCode

        value = normalize(value)
        if not re.fullmatch(r"[A-Z0-9-]{6,40}", value):
            raise ValidationError("6 to 40 letters, digits or dashes.")
        clash = InviteCode.objects.filter(code__iexact=value)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise ValidationError("That code already exists.")
        return value


class InviteCodesView(StaffView):
    """GET the invite codes (with who used them); POST a new one (empty code = generated)."""

    def get(self, request):
        from accounts.models import InviteCode

        out = []
        for c in InviteCode.objects.prefetch_related("redemptions__user"):
            data = InviteCodeSerializer(c).data
            data["link"] = f"{conf.get('PUBLIC_URL')}/register?code={c.code}"
            data["redeemed_by"] = [{"email": r.user.email, "name": r.user.full_name, "at": r.created_at} for r in c.redemptions.all()[:200]]
            out.append(data)
        return Response(out)

    def post(self, request):
        s = InviteCodeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        c = s.save(created_by=request.user)
        return Response({**InviteCodeSerializer(c).data, "link": f"{conf.get('PUBLIC_URL')}/register?code={c.code}", "redeemed_by": []}, status=201)


class InviteCodeView(StaffView):
    """PATCH (active, max uses, validity, note); DELETE only while unused (otherwise switch it off)."""

    def patch(self, request, pk):
        from accounts.models import InviteCode

        c = get_object_or_404(InviteCode, pk=pk)
        data = {k: v for k, v in request.data.items() if k in ("active", "max_uses", "valid_until", "note")}
        s = InviteCodeSerializer(c, data=data, partial=True)
        s.is_valid(raise_exception=True)
        return Response(InviteCodeSerializer(s.save()).data)

    def delete(self, request, pk):
        from accounts.models import InviteCode

        c = get_object_or_404(InviteCode, pk=pk)
        if c.uses:
            raise ValidationError({"detail": "This code was already used: switch it off instead (what people got stays)."})
        c.delete()
        return Response(status=204)
