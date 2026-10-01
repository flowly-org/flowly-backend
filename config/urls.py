from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from tasks.discussion import PublicConfigView
from tasks.sharing import SharedWithMeView, TaskLinkAttachmentView, TaskLinkCommentView, TaskLinkView
from tasks.views import MyContextView, MyTaskListView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/admin/", include("instance.urls")),
    path("api/config/", PublicConfigView.as_view(), name="public-config"),
    path("api/t/<str:ref>/", TaskLinkView.as_view(), name="task-link"),
    path("api/t/<str:ref>/comments/", TaskLinkCommentView.as_view(), name="task-link-comments"),
    path("api/t/<str:ref>/attachments/<int:attachment_id>/download/", TaskLinkAttachmentView.as_view(), name="task-link-attachment"),
    path("api/my/shared/", SharedWithMeView.as_view(), name="my-shared"),
    path("api/my/tasks/", MyTaskListView.as_view(), name="my-tasks"),
    path("api/my/context/", MyContextView.as_view(), name="my-context"),
    path("api/", include("workspaces.urls")),
    path("api/", include("integrations.urls")),
    path("api/", include("concierge.urls")),
    path("api/workspaces/<int:workspace_pk>/", include("tasks.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
