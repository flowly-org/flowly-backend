from datetime import timedelta

from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from integrations.models import Integration

from instance import conf

from . import brain, telegram
from .bot import handle_update
from .i18n import LANGUAGES
from .personality import PERSONALITIES, choices as personality_choices
from .models import AIUsage, Notification


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "kind", "title", "body", "workspace", "task", "data", "created_at", "read_at"]


class NotificationsView(APIView):
    """GET latest notifications (?unread=1); POST {"ids": [...]} or {"all": true} marks them read."""

    def get(self, request):
        qs = Notification.objects.filter(user=request.user)
        if request.query_params.get("unread"):
            qs = qs.filter(read_at__isnull=True)
        after = request.query_params.get("after")
        if after and after.isdigit():
            qs = qs.filter(pk__gt=int(after))
        return Response(
            {
                "unread": Notification.objects.filter(user=request.user, read_at__isnull=True).count(),
                "results": NotificationSerializer(qs[:50], many=True).data,
            }
        )

    def post(self, request):
        qs = Notification.objects.filter(user=request.user, read_at__isnull=True)
        if not request.data.get("all"):
            qs = qs.filter(pk__in=[int(i) for i in request.data.get("ids", [])])
        qs.update(read_at=timezone.now())
        return Response({"unread": Notification.objects.filter(user=request.user, read_at__isnull=True).count()})


class ConciergeSettingsSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    name = serializers.CharField(required=False, max_length=40, allow_blank=False)
    language = serializers.ChoiceField(required=False, choices=list(LANGUAGES))
    timezone = serializers.CharField(required=False, max_length=64)
    brief_time = serializers.TimeField(required=False)
    review_day = serializers.ChoiceField(required=False, choices=["fri", "sun", "off"])
    review_time = serializers.TimeField(required=False)
    morning_brief = serializers.BooleanField(required=False)
    alerts = serializers.BooleanField(required=False)
    provider = serializers.IntegerField(required=False, allow_null=True)
    personality = serializers.ChoiceField(required=False, choices=list(PERSONALITIES))
    learn = serializers.BooleanField(required=False)
    memory = serializers.CharField(required=False, allow_blank=True, max_length=4000)

    def validate_timezone(self, value):
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:
            raise serializers.ValidationError("Unknown time zone.") from exc
        return value


def provider_choices(user):
    return Integration.objects.filter(
        workspace__memberships__user=user, kind__in=["openrouter", "openai_compatible"], enabled=True
    ).select_related("workspace")


class ConciergeView(APIView):
    def get(self, request):
        user = request.user
        profile = brain.profile_for(user)
        used = sum(u.prompt_tokens + u.completion_tokens for u in AIUsage.objects.filter(user=user, date=timezone.localdate()))
        bot_name = ""
        if telegram.enabled():
            try:
                bot_name = telegram.bot_username()
            except Exception:
                bot_name = ""
        return Response(
            {
                "plan": user.plan,
                "has_pro": user.has_pro,
                "available": brain.availability(user) is None,
                "unavailable_reason": brain.availability(user),
                "platform_ai": bool(conf.get("OPENROUTER_API_KEY")),
                "telegram": {
                    "configured": telegram.enabled(),
                    "bot": bot_name,
                    "linked": bool(profile.telegram_chat_id),
                    "username": profile.telegram_username,
                },
                "languages": [{"code": k, "label": v} for k, v in LANGUAGES.items()],
                "settings": {
                    "enabled": profile.enabled,
                    "name": profile.name,
                    "language": profile.language,
                    "timezone": profile.timezone,
                    "brief_time": profile.brief_time.strftime("%H:%M"),
                    "review_day": profile.review_day,
                    "review_time": profile.review_time.strftime("%H:%M"),
                    "morning_brief": profile.morning_brief,
                    "alerts": profile.alerts,
                    "provider": profile.provider_id,
                    "personality": profile.personality,
                    "learn": profile.learn,
                },
                "personalities": personality_choices(),
                "memory_updated_at": profile.memory_updated_at,
                "providers": [
                    {"id": i.id, "name": i.name, "workspace": i.workspace.name, "kind": i.kind, "model": i.config.get("model")}
                    for i in provider_choices(user)
                ],
                "memory": profile.memory,
                "usage_today": used,
                "daily_limit": conf.get("CONCIERGE_DAILY_TOKENS"),
            }
        )

    def patch(self, request):
        data = ConciergeSettingsSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        profile = brain.profile_for(request.user)
        values = dict(data.validated_data)
        if "provider" in values:
            pid = values.pop("provider")
            if pid is not None and not provider_choices(request.user).filter(pk=pid).exists():
                raise serializers.ValidationError({"provider": "Pick an AI provider from one of your workspaces."})
            profile.provider_id = pid
        for key, value in values.items():
            setattr(profile, key, value)
        profile.save()
        return self.get(request)

    def delete(self, request):
        """Forget the memory and chat history."""
        profile = brain.profile_for(request.user)
        profile.memory = ""
        profile.save(update_fields=["memory"])
        request.user.concierge_messages.all().delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TelegramLinkView(APIView):
    def post(self, request):
        if not telegram.enabled():
            return Response({"detail": "The Telegram bot isn't configured on this server."}, status=400)
        profile = brain.profile_for(request.user)
        code = profile.new_link_code()
        profile.link_expires_at = timezone.now() + timedelta(minutes=30)
        profile.save(update_fields=["link_code", "link_expires_at"])
        return Response({"url": f"https://t.me/{telegram.bot_username()}?start={code}", "expires_at": profile.link_expires_at})

    def delete(self, request):
        profile = brain.profile_for(request.user)
        profile.telegram_chat_id = None
        profile.telegram_username = ""
        profile.save(update_fields=["telegram_chat_id", "telegram_username"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConciergeChatView(APIView):
    """Talk to the concierge from the web app."""

    def post(self, request):
        text = (request.data.get("message") or "").strip()
        if not text:
            return Response({"detail": "Say something."}, status=400)
        return Response({"reply": brain.reply(request.user, text[:2000], channel="web")})


class ConciergeBriefView(APIView):
    def post(self, request):
        text = brain.brief(request.user)
        profile = brain.profile_for(request.user)
        if profile.telegram_chat_id and request.data.get("send"):
            telegram.send_message(profile.telegram_chat_id, text)
        return Response({"brief": text})


class TelegramWebhookView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        secret = conf.get("TELEGRAM_WEBHOOK_SECRET")
        if not secret or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        handle_update(request.data)
        return Response({"ok": True})
