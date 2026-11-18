"""Rutas WebSocket de monitoring (las rutas HTTP están en urls.py)."""

from django.urls import path

from monitoring.consumers import SessionConsumer

websocket_urlpatterns = [
    path('ws/sesion/', SessionConsumer.as_asgi()),
]
