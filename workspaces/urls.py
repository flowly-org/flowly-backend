from django.urls import include, path
from rest_framework.routers import DefaultRouter, SimpleRouter

from .views import InvitationViewSet, MemberViewSet, WorkspaceViewSet

router = DefaultRouter()
router.register("workspaces", WorkspaceViewSet, basename="workspace")

nested = SimpleRouter()
nested.register("members", MemberViewSet, basename="member")
nested.register("invitations", InvitationViewSet, basename="invitation")

urlpatterns = [
    path("", include(router.urls)),
    path("workspaces/<int:workspace_pk>/", include(nested.urls)),
]
