from rest_framework.routers import SimpleRouter

from integrations.views import IntegrationViewSet

from django.urls import include, path

from .discussion import AttachmentViewSet, CommentViewSet
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

urlpatterns = router.urls + [path("tasks/<int:task_pk>/", include(task_children.urls))]
