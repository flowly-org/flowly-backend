"""The Flowly Slack app (Enterprise): install per workspace, events, /flowly, channel picker, manifest."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from django.core import signing
from django.shortcuts import get_object_or_404, redirect
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from instance import conf

from .connectors.teamchat import SlackBot
from .crypto import encrypt
from .http import DeliveryError, send
from .models import Integration

SCOPES = ["app_mentions:read", "chat:write", "chat:write.public", "channels:read", "commands", "im:history", "im:read",
          "im:write", "users:read", "users:read.email"]
STATE_SALT = "flowly.slack.install"


def api_url(path: str) -> str:
    return f"{conf.get('API_PUBLIC_URL')}{path}"


def redirect_uri() -> str:
    return api_url("/api/integrations/slack/oauth/callback/")


def enabled() -> bool:
    return bool(conf.get("SLACK_CLIENT_ID") and conf.get("SLACK_CLIENT_SECRET") and conf.get("SLACK_SIGNING_SECRET"))


def manifest() -> dict:
    return {
        "display_information": {"name": "Flowly", "description": "Your team's day in Flowly: today's tasks, updates and an AI assistant.",
                                "background_color": "#4f46e5"},
        "features": {
            "app_home": {"messages_tab_enabled": True, "messages_tab_read_only_enabled": False},
            "bot_user": {"display_name": "Flowly", "always_online": True},
            "slash_commands": [{"command": "/flowly", "url": api_url("/api/integrations/slack/commands/"),
                                "description": "Today's tasks, yours, or ask Flowly anything", "usage_hint": "today | mine | <question>",
                                "should_escape": False}],
        },
        "oauth_config": {"redirect_urls": [redirect_uri()], "scopes": {"bot": SCOPES}},
        "settings": {"event_subscriptions": {"request_url": api_url("/api/integrations/slack/events/"),
                                             "bot_events": ["app_mention", "message.im"]},
                     "org_deploy_enabled": False, "socket_mode_enabled": False, "token_rotation_enabled": False},
    }


def verify(request) -> bool:
    """Slack's request signature: v0=HMAC-SHA256(signing secret, "v0:{timestamp}:{body}"), at most 5 minutes old."""
    secret = conf.get("SLACK_SIGNING_SECRET")
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not secret or not ts.isdigit() or abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:".encode() + request.body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def integrations_for(team_id):
    return Integration.objects.filter(kind="slack_bot", enabled=True, config__team_id=team_id).select_related("workspace", "created_by")


class SlackInstallView(APIView):
    """POST {"workspace": id} → {"url"}: Slack's "Add to Slack" page for that Flowly workspace."""

    def post(self, request):
        from workspaces.models import Membership

        membership = Membership.objects.filter(workspace_id=request.data.get("workspace"), user=request.user).select_related("workspace").first()
        if membership is None or not membership.can_manage:
            return Response({"detail": "Only workspace owners and admins can connect Slack."}, status=status.HTTP_403_FORBIDDEN)
        if not membership.workspace.is_enterprise:
            return Response({"detail": "The Slack app needs the Enterprise plan for this workspace."}, status=status.HTTP_402_PAYMENT_REQUIRED)
        if not enabled():
            return Response({"detail": "The Slack app isn't set up on this server yet (Admin → Settings → Slack app)."}, status=400)
        state = signing.dumps({"w": membership.workspace_id, "u": request.user.id}, salt=STATE_SALT)
        url = "https://slack.com/oauth/v2/authorize?" + urlencode(
            {"client_id": conf.get("SLACK_CLIENT_ID"), "scope": ",".join(SCOPES), "redirect_uri": redirect_uri(), "state": state}
        )
        return Response({"url": url})


class SlackOAuthCallbackView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from accounts.models import User
        from workspaces.models import Workspace

        app = conf.get("PUBLIC_URL")
        try:
            state = signing.loads(request.query_params.get("state", ""), salt=STATE_SALT, max_age=1800)
        except signing.BadSignature:
            return redirect(f"{app}/?slack=error")
        back = f"{app}/w/{state['w']}/integrations"
        if request.query_params.get("error"):
            return redirect(f"{back}?slack=cancelled")
        try:
            res = send("POST", "https://slack.com/api/oauth.v2.access", check_url=False, data={
                "client_id": conf.get("SLACK_CLIENT_ID"), "client_secret": conf.get("SLACK_CLIENT_SECRET"),
                "code": request.query_params.get("code", ""), "redirect_uri": redirect_uri(),
            }).json()
        except DeliveryError:
            return redirect(f"{back}?slack=error")
        if not res.get("ok"):
            return redirect(f"{back}?slack=error")
        ws = get_object_or_404(Workspace, pk=state["w"])
        user = User.objects.filter(pk=state["u"]).first()
        team = res.get("team") or {}
        integration = Integration.objects.filter(workspace=ws, kind="slack_bot", config__team_id=team.get("id")).first()
        if integration is None:
            integration = Integration(workspace=ws, kind="slack_bot", name=f"Slack · {team.get('name', '')}"[:120], created_by=user)
        integration.config = {**SlackBot().clean_config({}, integration if integration.pk else None),
                              **{k: v for k, v in integration.config.items()},
                              "team_id": team.get("id"), "team_name": team.get("name", ""), "bot_user_id": res.get("bot_user_id", "")}
        integration.credentials = {**integration.credentials, "bot_token": encrypt(res["access_token"])}
        integration.enabled = True
        integration.save()
        return redirect(f"{back}?slack=connected")


class SlackEventsView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if not verify(request):
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        body = json.loads(request.body or b"{}")
        if body.get("type") == "url_verification":
            return Response({"challenge": body.get("challenge")})
        if request.headers.get("X-Slack-Retry-Num"):
            return Response(status=status.HTTP_200_OK)  # we answer asynchronously: a retry would answer twice
        event = body.get("event") or {}
        kind = event.get("type")
        is_dm = kind == "message" and event.get("channel_type") == "im" and not event.get("bot_id") and not event.get("subtype")
        if kind == "app_mention" or is_dm:
            from concierge.models import BotJob

            for integration in integrations_for(body.get("team_id"))[:1]:
                BotJob.objects.create(kind="slack_event", integration=integration, payload={"event": event})
        return Response(status=status.HTTP_200_OK)


class SlackCommandView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if not verify(request):
            return Response(status=status.HTTP_401_UNAUTHORIZED)
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(request.body.decode()).items()}
        integration = integrations_for(form.get("team_id")).first()
        if integration is None:
            return Response({"response_type": "ephemeral", "text": "This Slack workspace isn't connected to Flowly."})
        from concierge.models import BotJob

        BotJob.objects.create(kind="slack_command", integration=integration, payload=form)
        return Response({"response_type": "ephemeral", "text": "⏳ On it…"})


class SlackChannelsView(APIView):
    """GET → public channels of the connected Slack workspace, for picking where briefs and updates go."""

    def get(self, request, ws, pk):
        integration = get_object_or_404(Integration, pk=pk, workspace_id=ws, kind="slack_bot",
                                        workspace__memberships__user=request.user)
        try:
            res = SlackBot.api(integration, "conversations.list", types="public_channel", exclude_archived=True, limit=500)
        except DeliveryError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response([{"id": c["id"], "name": c["name"]} for c in res.get("channels", [])])


class SlackManifestView(APIView):
    """Instance admins: the app manifest to paste into api.slack.com → Create app → From a manifest."""

    def get(self, request):
        if not request.user.is_staff:
            return Response(status=status.HTTP_403_FORBIDDEN)
        return Response({"manifest": manifest(), "configured": enabled()})
