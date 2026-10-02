from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from accounts.guide import GuideView
from concierge.photo import PhotoTasksView
from mcpauth import views as oauth
from mcpserver.views import McpView
from timetracking.views import InvoicingProfileView, TimerView
from tasks.discussion import PublicConfigView
from tasks.sharing import SharedWithMeView, TaskLinkAttachmentView, TaskLinkCommentView, TaskLinkView
from tasks.views import MyContextView, MyTaskListView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/admin/", include("instance.urls")),
    path("api/mcp/", McpView.as_view(), name="mcp"),
    path("api/mcp", McpView.as_view()),
    path("api/mcp/w/<int:workspace_id>/", McpView.as_view(), name="mcp-workspace"),
    path("api/mcp/w/<int:workspace_id>", McpView.as_view()),
    path("api/workspaces/<int:ws>/tasks/from-photo/", PhotoTasksView.as_view(), name="tasks-from-photo"),
    path("api/workspaces/<int:ws>/mcp-tokens/", oauth.WorkspaceTokensView.as_view(), name="workspace-mcp-tokens"),
    path("api/workspaces/<int:ws>/mcp-tokens/<int:pk>/", oauth.WorkspaceTokensView.as_view(), name="workspace-mcp-token"),
    path(".well-known/oauth-protected-resource", oauth.ProtectedResourceMetadata.as_view()),
    path(".well-known/oauth-protected-resource/<path:path>", oauth.ProtectedResourceMetadata.as_view()),
    path(".well-known/oauth-authorization-server", oauth.AuthorizationServerMetadata.as_view()),
    path(".well-known/oauth-authorization-server/<path:path>", oauth.AuthorizationServerMetadata.as_view()),
    path(".well-known/openid-configuration", oauth.AuthorizationServerMetadata.as_view()),
    path("api/oauth/register", oauth.RegisterView.as_view(), name="oauth-register"),
    path("api/oauth/token", oauth.TokenView.as_view(), name="oauth-token"),
    path("api/oauth/revoke", oauth.RevokeView.as_view(), name="oauth-revoke"),
    path("api/oauth/authorize/", oauth.AuthorizeView.as_view(), name="oauth-authorize"),
    path("api/oauth/grants/", oauth.GrantsView.as_view(), name="oauth-grants"),
    path("api/oauth/grants/<int:pk>/", oauth.GrantsView.as_view(), name="oauth-grant"),
    path("mcp", McpView.as_view()),
    path("api/config/", PublicConfigView.as_view(), name="public-config"),
    path("api/t/<str:ref>/", TaskLinkView.as_view(), name="task-link"),
    path("api/t/<str:ref>/comments/", TaskLinkCommentView.as_view(), name="task-link-comments"),
    path("api/t/<str:ref>/attachments/<int:attachment_id>/download/", TaskLinkAttachmentView.as_view(), name="task-link-attachment"),
    path("api/my/guide/", GuideView.as_view(), name="my-guide"),
    path("api/my/invoicing/", InvoicingProfileView.as_view(), name="my-invoicing"),
    path("api/my/timer/", TimerView.as_view(), name="my-timer"),
    path("api/my/shared/", SharedWithMeView.as_view(), name="my-shared"),
    path("api/my/tasks/", MyTaskListView.as_view(), name="my-tasks"),
    path("api/my/context/", MyContextView.as_view(), name="my-context"),
    path("api/", include("workspaces.urls")),
    path("api/", include("integrations.urls")),
    path("api/", include("concierge.urls")),
    path("api/", include("calendars.urls")),
    path("api/workspaces/<int:workspace_pk>/", include("tasks.urls")),
    path("api/workspaces/<int:workspace_pk>/", include("timetracking.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
