from django.urls import path

from . import feed, views

urlpatterns = [
    path("calendar/", views.CalendarOverviewView.as_view(), name="calendar"),
    path("calendar/google/connect/", views.GoogleConnectView.as_view(), name="calendar-google-connect"),
    path("calendar/google/callback/", views.GoogleCallbackView.as_view(), name="calendar-google-callback"),
    path("calendar/microsoft/connect/", views.MicrosoftConnectView.as_view(), name="calendar-microsoft-connect"),
    path("calendar/microsoft/callback/", views.MicrosoftCallbackView.as_view(), name="calendar-microsoft-callback"),
    path("calendar/caldav/", views.CalDavAddView.as_view(), name="calendar-caldav"),
    path("calendar/ics/", views.IcsAddView.as_view(), name="calendar-ics"),
    path("calendar/feed/", feed.FeedSettingsView.as_view(), name="calendar-feed"),
    path("ical/<str:token>.ics", feed.FeedView.as_view(), name="calendar-feed-ics"),
    path("my/blocks/", views.BlocksView.as_view(), name="blocks"),
    path("my/blocks/plan/", views.PlanDayView.as_view(), name="blocks-plan"),
    path("my/blocks/<int:pk>/", views.BlockView.as_view(), name="block"),
    path("calendar/accounts/<int:pk>/", views.AccountView.as_view(), name="calendar-account"),
    path("calendar/calendars/<int:pk>/", views.CalendarView.as_view(), name="calendar-calendar"),
    path("calendar/events/", views.EventListView.as_view(), name="calendar-events"),
    path("calendar/events/<int:pk>/", views.EventView.as_view(), name="calendar-event"),
    path("calendar/events/<int:pk>/tasks/", views.EventTasksView.as_view(), name="calendar-event-tasks"),
    path("calendar/events/<int:pk>/tasks/<int:task_id>/", views.EventTasksView.as_view(), name="calendar-event-task"),
]
