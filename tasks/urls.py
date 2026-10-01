from rest_framework.routers import SimpleRouter

from .views import ProjectViewSet, TagGroupViewSet, TagViewSet, TaskViewSet

router = SimpleRouter()
router.register("projects", ProjectViewSet, basename="project")
router.register("tag-groups", TagGroupViewSet, basename="tag-group")
router.register("tags", TagViewSet, basename="tag")
router.register("tasks", TaskViewSet, basename="task")

urlpatterns = router.urls
