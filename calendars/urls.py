from django.urls import path

from . import views

urlpatterns = [
    path("calendar/", views.CalendarOverviewView.as_view(), name="calendar"),
    path("calendar/google/connect/", views.GoogleConnectView.as_view(), name="calendar-google-connect"),
    path("calendar/google/callback/", views.GoogleCallbackView.as_view(), name="calendar-google-callback"),
    path("calendar/accounts/<int:pk>/", views.AccountView.as_view(), name="calendar-account"),
    path("calendar/calendars/<int:pk>/", views.CalendarView.as_view(), name="calendar-calendar"),
    path("calendar/events/", views.EventListView.as_view(), name="calendar-events"),
    path("calendar/events/<int:pk>/", views.EventView.as_view(), name="calendar-event"),
    path("calendar/events/<int:pk>/tasks/", views.EventTasksView.as_view(), name="calendar-event-tasks"),
    path("calendar/events/<int:pk>/tasks/<int:task_id>/", views.EventTasksView.as_view(), name="calendar-event-task"),
]
