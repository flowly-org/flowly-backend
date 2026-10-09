from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .serializers import LoginSerializer
from .views import ApiTokensView, CodeCheckView, MeView, RedeemView, RegisterView

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", TokenObtainPairView.as_view(serializer_class=LoginSerializer), name="login"),
    path("refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("me/", MeView.as_view(), name="me"),
    path("code/", CodeCheckView.as_view(), name="code-check"),
    path("redeem/", RedeemView.as_view(), name="redeem"),
    path("tokens/", ApiTokensView.as_view(), name="api-tokens"),
    path("tokens/<int:pk>/", ApiTokensView.as_view(), name="api-token"),
]
