from django.urls import path

from . import views

urlpatterns = [
    path("workspaces/<int:workspace_pk>/tasks/<int:pk>/stairway/", views.TaskRunsView.as_view(), name="stairway-runs"),
    path("workspaces/<int:workspace_pk>/tasks/<int:pk>/stairway/<int:run_pk>/<str:action>/", views.RunActionView.as_view(),
         name="stairway-run-action"),
    path("stairway/callback/<str:token>/", views.CallbackView.as_view(), name="stairway-callback"),
]
