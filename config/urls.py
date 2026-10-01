from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from tasks.views import MyContextView, MyTaskListView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/my/tasks/", MyTaskListView.as_view(), name="my-tasks"),
    path("api/my/context/", MyContextView.as_view(), name="my-context"),
    path("api/", include("workspaces.urls")),
    path("api/", include("integrations.urls")),
    path("api/workspaces/<int:workspace_pk>/", include("tasks.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]
