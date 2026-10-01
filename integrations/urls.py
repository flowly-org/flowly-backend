from django.urls import path

from . import views

urlpatterns = [
    path("hooks/<str:token>/", views.IncomingHookView.as_view(), name="incoming-hook"),
    path("integrations/email/inbound/", views.EmailInboundView.as_view(), name="email-inbound"),
    path("integrations/github/webhook/", views.GitHubWebhookView.as_view(), name="github-webhook"),
    path("integrations/github/app/", views.GitHubAppView.as_view(), name="github-app"),
    path("integrations/github/app/manifest/", views.GitHubManifestView.as_view(), name="github-manifest"),
    path("integrations/github/app/convert/", views.GitHubConvertView.as_view(), name="github-convert"),
    path("integrations/github/installed/", views.GitHubInstalledView.as_view(), name="github-installed"),
]
