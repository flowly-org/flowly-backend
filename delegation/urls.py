from django.urls import path

from . import views

urlpatterns = [
    path("workspaces/<int:ws>/tasks/<int:pk>/delegation/", views.TaskDelegationView.as_view(), name="task-delegation"),
]
