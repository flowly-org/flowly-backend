from rest_framework.routers import SimpleRouter

from integrations.views import IntegrationViewSet

from .views import ProjectViewSet, TagGroupViewSet, TagViewSet, TaskViewSet

router = SimpleRouter()
router.register("projects", ProjectViewSet, basename="project")
router.register("tag-groups", TagGroupViewSet, basename="tag-group")
router.register("tags", TagViewSet, basename="tag")
router.register("tasks", TaskViewSet, basename="task")
router.register("integrations", IntegrationViewSet, basename="integration")

urlpatterns = router.urls
