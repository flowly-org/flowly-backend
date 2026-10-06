from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from workspaces.services import onboard_user

from .serializers import RegisterSerializer, UserSerializer


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        onboard_user(user)
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "user": UserSerializer(user).data,
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_201_CREATED,
        )


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class ApiTokensView(APIView):
    """GET list · POST {name} → the token is returned once · DELETE …/<id>/ revokes."""

    def get(self, request):
        from .tokens import ApiTokenSerializer

        return Response(ApiTokenSerializer(request.user.api_tokens.all(), many=True).data)

    def post(self, request):
        from .models import ApiToken
        from .tokens import ApiTokenSerializer

        name = (request.data.get("name") or "").strip()[:80] or "API token"
        if request.user.api_tokens.count() >= 20:
            return Response({"detail": "You have 20 tokens already; revoke one first."}, status=400)
        token, raw = ApiToken.issue(request.user, name)
        return Response({**ApiTokenSerializer(token).data, "token": raw}, status=status.HTTP_201_CREATED)

    def delete(self, request, pk):
        get_object_or_404(request.user.api_tokens, pk=pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CodeCheckView(APIView):
    """GET /api/auth/code/?code=… → what the code gives (for the sign-up form). Public, rate limited."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invite_code"

    def get(self, request):
        from .codes import CodeError, describe, find

        try:
            return Response({"valid": True, **describe(find(request.query_params.get("code", "")))})
        except CodeError as exc:
            return Response({"valid": False, "detail": str(exc)})


class RedeemView(APIView):
    """POST /api/auth/redeem/ {code} → apply an invite code to my account."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "invite_code"

    def post(self, request):
        from .codes import CodeError, has_paid_subscription, redeem

        try:
            r = redeem(request.user, str(request.data.get("code") or ""))
        except CodeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        request.user.refresh_from_db()
        stripe_live = has_paid_subscription(request.user)
        return Response({"plan": r.plan, "forever": r.plan_expires_at is None, "user": UserSerializer(request.user).data,
                         "stripe_subscription": stripe_live})
