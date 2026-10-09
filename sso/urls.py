from django.urls import path

from . import views

urlpatterns = [
    path("auth/sso/discover/", views.DiscoverView.as_view(), name="sso-discover"),
    path("auth/sso/start/", views.StartView.as_view(), name="sso-start"),
    path("auth/sso/callback/", views.CallbackView.as_view(), name="sso-callback"),
    path("workspaces/<int:workspace_pk>/sso/", views.ConnectionView.as_view(), name="sso-connection"),
    path("workspaces/<int:workspace_pk>/sso/domains/", views.DomainsView.as_view(), name="sso-domains"),
]
