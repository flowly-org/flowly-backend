from rest_framework.routers import SimpleRouter

from integrations.views import IntegrationViewSet

from django.urls import include, path

from .discussion import AttachmentViewSet, CommentViewSet
from .sharing import BulkCreateView, TaskSharesView
from .views import ProjectViewSet, TagGroupViewSet, TagViewSet, TaskViewSet

router = SimpleRouter()
router.register("projects", ProjectViewSet, basename="project")
router.register("tag-groups", TagGroupViewSet, basename="tag-group")
router.register("tags", TagViewSet, basename="tag")
router.register("tasks", TaskViewSet, basename="task")
router.register("integrations", IntegrationViewSet, basename="integration")

task_children = SimpleRouter()
task_children.register("comments", CommentViewSet, basename="comment")
task_children.register("attachments", AttachmentViewSet, basename="attachment")

urlpatterns = [
    # Before the router so "bulk" isn't taken for a task id.
    path("tasks/bulk/", BulkCreateView.as_view(), name="task-bulk"),
    path("tasks/<int:task_pk>/shares/", TaskSharesView.as_view(), name="task-shares"),
    path("tasks/<int:task_pk>/shares/<int:share_id>/", TaskSharesView.as_view(), name="task-share"),
    *router.urls,
    path("tasks/<int:task_pk>/", include(task_children.urls)),
]
