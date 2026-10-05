from django.urls import path

from . import views

urlpatterns = [
    path("my/review/", views.ReviewView.as_view(), name="my-review"),
    path("insights/", views.InsightsView.as_view(), name="insights"),
]
