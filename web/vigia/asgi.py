"""Punto de entrada ASGI de VIGÍA.

- HTTP: las vistas normales de Django.
- WebSocket: los datos de la sesión en tiempo real (monitoring/consumers.py).

`runserver` lo usa automáticamente porque `daphne` está en INSTALLED_APPS.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vigia.settings')

# Django debe iniciarse antes de importar código que use modelos o ajustes.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from monitoring.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(URLRouter(websocket_urlpatterns)),
})
