from django.urls import path

from . import views

urlpatterns = [
    path("hooks/donetick/<str:token>/", views.HookView.as_view(), name="donetick-hook"),
    path("my/donetick/", views.AccountsView.as_view(), name="my-donetick"),
    path("my/donetick/<int:pk>/", views.AccountView.as_view(), name="my-donetick-account"),
    path("my/donetick/<int:pk>/sync/", views.AccountView.as_view(), name="my-donetick-sync"),
]
