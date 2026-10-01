import json

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from workspaces.models import Membership
from workspaces.permissions import WorkspaceScopedMixin

from . import github
from .connectors import REGISTRY
from .connectors.email import Email, parse_address, token_from_recipient
from .connectors.github import GitHub
from .connectors.webhooks import sign
from .http import DeliveryError
from .inbound import find_member, parse_date, priority_by_name, status_by_name, tags_by_names, upsert_task
from .models import Delivery, Integration, new_secret
from .serializers import DeliverySerializer, IntegrationSerializer
from .worker import run_one

INSTALL_SALT = "flowly.github.install"
MANIFEST_SALT = "flowly.github.manifest"


def public_base_url(request):
    return settings.PUBLIC_URL or f"{request.scheme}://{request.get_host()}"


class IntegrationViewSet(WorkspaceScopedMixin, viewsets.ModelViewSet):
    """Integrations of a workspace (optionally ?project=<id>). Owners and admins only."""

    serializer_class = IntegrationSerializer

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.require_manager()

    def get_queryset(self):
        qs = self.workspace.integrations.select_related("workspace")
        project = self.request.query_params.get("project")
        if project and project.isdigit():
            qs = qs.filter(project_id=int(project))
        return qs

    def perform_create(self, serializer):
        serializer.save(workspace=self.workspace, created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def test(self, request, workspace_pk=None, pk=None):
        integration = self.get_object()
        connector = REGISTRY[integration.kind]
        if not connector.outgoing:
            raise ValidationError({"detail": "This integration only receives data; send something to its URL or address to test it."})
        delivery = Delivery.objects.create(
            integration=integration, direction=Delivery.Direction.OUT, event="ping",
            payload=connector.test_payload(integration), next_attempt_at=timezone.now(),
        )
        delivery = run_one(delivery)
        if delivery.status == Delivery.Status.PENDING:  # a failed test isn't worth retrying
            delivery.status = Delivery.Status.FAILED
            delivery.finished_at = timezone.now()
            delivery.save()
        return Response(DeliverySerializer(delivery).data)

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, workspace_pk=None, pk=None):
        integration = self.get_object()
        integration.secret = new_secret()
        integration.save(update_fields=["secret"])
        return Response(self.get_serializer(integration).data)

    @action(detail=True, methods=["get"])
    def deliveries(self, request, workspace_pk=None, pk=None):
        integration = self.get_object()
        return Response(DeliverySerializer(integration.deliveries.all()[:50], many=True).data)

    @action(detail=True, methods=["post"], url_path=r"deliveries/(?P<delivery_id>\d+)/retry")
    def retry(self, request, workspace_pk=None, pk=None, delivery_id=None):
        integration = self.get_object()
        delivery = get_object_or_404(integration.deliveries, pk=delivery_id, direction=Delivery.Direction.OUT)
        delivery.status = Delivery.Status.PENDING
        delivery.next_attempt_at = timezone.now()
        delivery.save(update_fields=["status", "next_attempt_at"])
        return Response(DeliverySerializer(run_one(delivery)).data)

    @action(detail=True, methods=["post"], url_path="github/install")
    def github_install(self, request, workspace_pk=None, pk=None):
        integration = self.get_object()
        if integration.kind != "github":
            raise ValidationError({"detail": "Not a GitHub integration."})
        app = github.get_app()
        if app is None:
            raise ValidationError({"detail": "The GitHub App isn't set up yet. An instance admin can create it from this page."})
        state = signing.dumps({"i": integration.id, "u": request.user.id}, salt=INSTALL_SALT)
        return Response({"url": f"{settings.GITHUB_WEB_URL}/apps/{app.slug}/installations/new?state={state}"})

    @action(detail=True, methods=["get"], url_path="github/repos")
    def github_repos(self, request, workspace_pk=None, pk=None):
        integration = self.get_object()
        inst = integration.config.get("installation_id")
        if integration.kind != "github" or not inst:
            raise ValidationError({"detail": "Install the GitHub App first."})
        try:
            data = github.installation_api(inst, "GET", "/installation/repositories?per_page=100")
        except DeliveryError as exc:
            raise ValidationError({"detail": f"GitHub: {exc}"}) from exc
        return Response(sorted(r["full_name"] for r in data.get("repositories", [])))


# --- GitHub App setup (instance-wide) -----------------------------------------


class GitHubAppView(APIView):
    def get(self, request):
        app = github.get_app()
        return Response(
            {
                "configured": app is not None,
                "can_setup": request.user.is_staff,
                "app": {"name": app.name, "slug": app.slug, "html_url": app.html_url, "owner": app.owner} if app else None,
            }
        )


class GitHubManifestView(APIView):
    """Returns the form the browser posts to GitHub to create the app (manifest flow)."""

    def post(self, request):
        if not request.user.is_staff:
            raise PermissionDenied("Only instance admins can create the GitHub App.")
        base = public_base_url(request)
        name = (request.data.get("name") or "Flowly").strip()[:34]
        org = (request.data.get("organization") or "").strip()
        target = f"{settings.GITHUB_WEB_URL}/organizations/{org}/settings/apps/new" if org else f"{settings.GITHUB_WEB_URL}/settings/apps/new"
        state = signing.dumps({"u": request.user.id}, salt=MANIFEST_SALT)
        return Response({"action": f"{target}?state={state}", "manifest": json.dumps(github.manifest(base, name))})


class GitHubConvertView(APIView):
    def post(self, request):
        if not request.user.is_staff:
            raise PermissionDenied("Only instance admins can create the GitHub App.")
        try:
            signing.loads(request.data.get("state", ""), salt=MANIFEST_SALT, max_age=3600)
        except signing.BadSignature as exc:
            raise ValidationError({"detail": "This setup link has expired; start again."}) from exc
        try:
            app = github.convert_manifest(request.data.get("code", ""))
        except DeliveryError as exc:
            raise ValidationError({"detail": f"GitHub refused the app setup: {exc} {exc.body}"}) from exc
        return Response({"name": app.name, "slug": app.slug, "html_url": app.html_url})


class GitHubInstalledView(APIView):
    """GitHub redirects here (via the frontend) after the app is installed on an account."""

    def post(self, request):
        try:
            state = signing.loads(request.data.get("state", ""), salt=INSTALL_SALT, max_age=3600)
        except signing.BadSignature as exc:
            raise ValidationError({"detail": "This install link has expired; start again from the integration."}) from exc
        if state["u"] != request.user.id:
            raise PermissionDenied("This install was started by someone else.")
        integration = get_object_or_404(Integration, pk=state["i"], kind="github")
        membership = Membership.objects.filter(workspace=integration.workspace, user=request.user).first()
        if not membership or not membership.can_manage:
            raise PermissionDenied("Only workspace owners and admins can configure integrations.")
        installation_id = int(request.data.get("installation_id") or 0)
        try:
            github.api("GET", f"/app/installations/{installation_id}", app_auth=True)
        except DeliveryError as exc:
            raise ValidationError({"detail": f"GitHub doesn't know this installation: {exc}"}) from exc
        integration.config = {**integration.config, "installation_id": installation_id}
        integration.save(update_fields=["config"])
        return Response({"workspace": integration.workspace_id, "integration": integration.id})


# --- Public receivers ---------------------------------------------------------


class PublicView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]


class GitHubWebhookView(PublicView):
    def post(self, request):
        app = github.get_app()
        body = request.body
        if app is None or not github.verify_signature(app.webhook_secret, body, request.headers.get("X-Hub-Signature-256")):
            return Response({"detail": "Bad signature."}, status=status.HTTP_401_UNAUTHORIZED)
        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return Response({"ok": True})
        deliveries = GitHub.handle_webhook(event, json.loads(body or b"{}"))
        return Response({"handled": len(deliveries)})


class IncomingHookView(PublicView):
    """POST /api/hooks/<token>/ — create or update a task from any system."""

    def post(self, request, token):
        integration = Integration.objects.filter(token=token, kind="incoming_webhook").select_related("workspace", "project").first()
        if integration is None or not integration.enabled:
            return Response({"detail": "Unknown or disabled hook."}, status=status.HTTP_404_NOT_FOUND)
        if integration.config.get("require_signature"):
            given = request.headers.get("X-Flowly-Signature", "")
            if not given or sign(integration.secret, request.body) != given:
                return Response({"detail": "Bad signature."}, status=status.HTTP_401_UNAUTHORIZED)
        data = request.data if isinstance(request.data, dict) else {}
        external_id = str(data.get("external_id") or "").strip() or None
        log = Delivery(
            integration=integration, direction=Delivery.Direction.IN, event="hook.received",
            payload=data, finished_at=timezone.now(),
        )
        fields = {}
        for key in ("title", "description"):
            if key in data:
                fields[key] = str(data[key] or "")[: 255 if key == "title" else 20000]
        for key in ("planned_date", "due_date"):
            if key in data:
                fields[key] = parse_date(data[key])
        if data.get("status"):
            found = status_by_name(integration, str(data["status"]))
            if found is None:
                return self._reject(log, f'Unknown status "{data["status"]}".')
            fields["status"] = found
        if "priority" in data:
            fields["priority"] = priority_by_name(integration, str(data["priority"] or ""))
        if "assignee" in data:
            fields["assignee"] = find_member(integration, email=str(data["assignee"] or "")) if data["assignee"] else None
        tags = tags_by_names(integration, data["tags"]) if isinstance(data.get("tags"), list) else None
        with transaction.atomic():
            from .inbound import linked_task

            if not fields.get("title") and not (external_id and linked_task(integration, external_id)):
                return self._reject(log, '"title" is required to create a task.')
            task, created = upsert_task(integration, fields, external_id=external_id, url=str(data.get("url") or "")[:500], tags=tags)
        log.task = task
        log.status = Delivery.Status.SUCCESS
        log.response_body = "Created task." if created else "Updated task."
        log.save()
        return Response({"task": task.id, "created": created}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @staticmethod
    def _reject(log, message):
        log.status = Delivery.Status.FAILED
        log.last_error = message
        log.save()
        return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)


class EmailInboundView(PublicView):
    """Inbound email in the formats of common providers (Cloudflare worker/JSON, Mailgun, SendGrid, Postmark)."""

    def post(self, request):
        key = settings.EMAIL_INBOUND_KEY
        if key and key not in (request.query_params.get("key"), request.headers.get("X-Flowly-Key")):
            return Response({"detail": "Bad key."}, status=status.HTTP_401_UNAUTHORIZED)
        d = request.data
        get = lambda *names: next((str(d.get(n)) for n in names if d.get(n)), "")
        recipients = [get("to", "To", "recipient")]
        if isinstance(d.get("ToFull"), list):
            recipients += [r.get("Email", "") for r in d["ToFull"]]
        recipients += [r for r in get("envelope_to").split(",")]
        sender = parse_address(get("from", "From", "sender"))
        subject = get("subject", "Subject")
        text = get("text", "TextBody", "body-plain", "stripped-text", "plain")
        message_id = get("message_id", "Message-Id", "MessageID", "Message-ID")

        handled = 0
        seen = set()
        for recipient in ",".join(recipients).split(","):
            token = token_from_recipient(recipient)
            if not token or token in seen:
                continue
            seen.add(token)
            integration = Integration.objects.filter(token=token, kind="email", enabled=True).select_related("workspace", "project").first()
            if integration:
                Email().receive(integration, sender=sender, subject=subject, text=text, message_id=message_id)
                handled += 1
        if not handled:
            return Response({"detail": "No integration matches the recipient."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"handled": handled})
