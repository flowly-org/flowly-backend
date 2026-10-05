from django.urls import path

from . import views

urlpatterns = [
    path("workspaces/<int:ws>/agile/", views.SetupsView.as_view(), name="agile-setups"),
    path("workspaces/<int:ws>/agile/<int:pk>/", views.SetupView.as_view(), name="agile-setup"),
    path("workspaces/<int:ws>/agile/<int:pk>/sprints/", views.SprintsView.as_view(), name="agile-sprints"),
    path("workspaces/<int:ws>/agile/<int:pk>/standup/", views.StandupView.as_view(), name="agile-standup"),
    path("workspaces/<int:ws>/sprints/<int:pk>/", views.SprintView.as_view(), name="sprint"),
    path("workspaces/<int:ws>/sprints/<int:pk>/tasks/", views.SprintTasksView.as_view(), name="sprint-tasks"),
    path("workspaces/<int:ws>/sprints/<int:pk>/close/", views.SprintCloseView.as_view(), name="sprint-close"),
    path("workspaces/<int:ws>/sprints/<int:pk>/retro/", views.RetroView.as_view(), name="sprint-retro"),
    path("workspaces/<int:ws>/retro/<int:pk>/", views.RetroItemView.as_view(), name="retro-item"),
]
