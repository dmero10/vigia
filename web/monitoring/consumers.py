"""WebSocket de la sesión: el servidor empuja los datos en tiempo real a la página.

El navegador abre `ws://<servidor>/ws/sesion/` y recibe mensajes JSON con el
estado (nivel, EAR, MAR, FPS, pulsera, M4) y el resumen en vivo. Sólo se envía
un mensaje cuando algo cambió, como máximo 10 veces por segundo.

Los botones siguen usando POST (vista `command`): el WebSocket sólo lleva datos
del servidor a la página. La cámara y el análisis siguen en el hilo del
orquestador; aquí sólo se lee su estado, que no bloquea.
"""

import asyncio
import json

from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.serializers.json import DjangoJSONEncoder

from monitoring.orchestrator import get_orchestrator
from monitoring.presenters import live_payload
from monitoring.registry_client import get_registry_queue

PUSH_INTERVAL_S = 0.1


class SessionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        self._task = asyncio.create_task(self._push())

    async def disconnect(self, code):
        task = getattr(self, '_task', None)
        if task is not None:
            task.cancel()

    async def _push(self):
        last = None
        while True:
            message = json.dumps(
                live_payload(get_orchestrator(), get_registry_queue()), cls=DjangoJSONEncoder
            )
            if message != last:
                await self.send(text_data=message)
                last = message
            await asyncio.sleep(PUSH_INTERVAL_S)
