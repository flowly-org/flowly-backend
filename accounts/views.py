from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.views import APIView
from rest_framework.response import Response
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
