from django.urls import path

from . import slack_app, views

urlpatterns = [
    path("hooks/<str:token>/", views.IncomingHookView.as_view(), name="incoming-hook"),
    path("integrations/email/inbound/", views.EmailInboundView.as_view(), name="email-inbound"),
    path("integrations/github/webhook/", views.GitHubWebhookView.as_view(), name="github-webhook"),
    path("integrations/github/app/", views.GitHubAppView.as_view(), name="github-app"),
    path("integrations/github/app/manifest/", views.GitHubManifestView.as_view(), name="github-manifest"),
    path("integrations/github/app/convert/", views.GitHubConvertView.as_view(), name="github-convert"),
    path("integrations/slack/install/", slack_app.SlackInstallView.as_view(), name="slack-install"),
    path("integrations/slack/oauth/callback/", slack_app.SlackOAuthCallbackView.as_view(), name="slack-oauth-callback"),
    path("integrations/slack/events/", slack_app.SlackEventsView.as_view(), name="slack-events"),
    path("integrations/slack/commands/", slack_app.SlackCommandView.as_view(), name="slack-commands"),
    path("integrations/slack/manifest/", slack_app.SlackManifestView.as_view(), name="slack-manifest"),
    path("workspaces/<int:ws>/integrations/<int:pk>/slack-channels/", slack_app.SlackChannelsView.as_view(), name="slack-channels"),
    path("integrations/github/installed/", views.GitHubInstalledView.as_view(), name="github-installed"),
]
