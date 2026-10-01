from django.urls import path

from . import views

urlpatterns = [
    path("notifications/", views.NotificationsView.as_view(), name="notifications"),
    path("concierge/", views.ConciergeView.as_view(), name="concierge"),
    path("concierge/telegram/link/", views.TelegramLinkView.as_view(), name="concierge-telegram-link"),
    path("concierge/telegram/webhook/", views.TelegramWebhookView.as_view(), name="concierge-telegram-webhook"),
    path("concierge/chat/", views.ConciergeChatView.as_view(), name="concierge-chat"),
    path("concierge/brief/", views.ConciergeBriefView.as_view(), name="concierge-brief"),
]
