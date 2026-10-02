from django.urls import path

from . import views

urlpatterns = [
    path("overview/", views.OverviewView.as_view(), name="admin-overview"),
    path("settings/", views.SettingsView.as_view(), name="admin-settings"),
    path("check/<str:service>/", views.CheckView.as_view(), name="admin-check"),
    path("telegram/webhook/", views.TelegramWebhookView.as_view(), name="admin-telegram-webhook"),
    path("github-app/", views.GitHubAppAdminView.as_view(), name="admin-github-app"),
    path("users/", views.UsersView.as_view(), name="admin-users"),
    path("users/<int:pk>/", views.UserDetailView.as_view(), name="admin-user"),
    path("workspaces/", views.WorkspacesView.as_view(), name="admin-workspaces"),
    path("workspaces/<int:pk>/", views.WorkspaceDetailView.as_view(), name="admin-workspace"),
]
