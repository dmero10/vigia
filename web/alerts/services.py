"""M3 - Lógica: envía el nivel a la pulsera sin bloquear el análisis (RF-07, RF-09).

El análisis sólo llama a `set_level()`, que no espera nada. Un hilo propio se
encarga de conectar, reintentar, enviar cada cambio y mandar un latido cada 1 s.
"""

import threading
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from alerts.bracelet import BraceletError, SimulatedBracelet

LEVELS = range(4)  # 0 = normal; 1, 2, 3 = alerta.
HEARTBEAT_INTERVAL_S = 1.0
RETRY_INTERVAL_S = 2.0


def create_bracelet(mode=None):
    """Crea la pulsera según `settings.BRACELET_MODE`."""
    mode = mode or settings.BRACELET_MODE
    if mode == 'simulated':
        return SimulatedBracelet()
    if mode == 'ble':
        from alerts.ble_client import BleBracelet  # Sólo se carga Bleak si se usa.
        return BleBracelet()
    raise ImproperlyConfigured(f'BRACELET_MODE desconocido: {mode!r}.')


@dataclass(frozen=True)
class AlertStatus:
    """Estado para mostrar en la interfaz."""

    connected: bool
    level: int  # Último nivel pedido por el análisis.
    sent_level: int | None  # Último nivel que llegó a la pulsera.
    last_error: str | None
    latency_ms: float | None  # Del último cambio: desde set_level() hasta enviarlo.


class AlertSender:
    """Mantiene la pulsera conectada y al día con el nivel actual."""

    def __init__(self, bracelet, heartbeat_interval=HEARTBEAT_INTERVAL_S,
                 retry_interval=RETRY_INTERVAL_S):
        self.bracelet = bracelet
        self._heartbeat_interval = heartbeat_interval
        self._retry_interval = retry_interval
        self._condition = threading.Condition()
        self._running = False
        self._thread = None
        self._level = 0
        self._level_changed_at = None
        self._sent_level = None
        self._connected = False
        self._last_error = None
        self._latency_ms = None

    def start(self):
        with self._condition:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._run, name='alert-sender', daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        """Envía nivel 0, desconecta la pulsera y espera a que termine el hilo."""
        with self._condition:
            self._running = False
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def set_level(self, level):
        """Pide enviar un nivel. No bloquea: el envío lo hace el hilo propio."""
        if level not in LEVELS:
            raise ValueError(f'Nivel inválido: {level}. Debe ser 0, 1, 2 o 3.')
        with self._condition:
            if level != self._level:
                self._level = level
                self._level_changed_at = time.monotonic()
                self._condition.notify_all()

    def status(self):
        with self._condition:
            return AlertStatus(
                connected=self._connected,
                level=self._level,
                sent_level=self._sent_level,
                last_error=self._last_error,
                latency_ms=self._latency_ms,
            )

    def _run(self):
        try:
            while self._is_running():
                if not self._connected and not self._connect():
                    self._wait(self._retry_interval)
                    continue
                self._send_next()
        finally:
            self._shutdown()

    def _is_running(self):
        with self._condition:
            return self._running

    def _wait(self, seconds):
        with self._condition:
            self._condition.wait_for(lambda: not self._running, timeout=seconds)

    def _connect(self):
        try:
            self.bracelet.connect()
        except BraceletError as error:
            self._mark_disconnected(error)
            return False
        with self._condition:
            self._connected = True
            self._last_error = None
            self._sent_level = None  # Al reconectar se reenvía el nivel actual.
            # Ese reenvío no cuenta como latencia: el tiempo sin conexión no es envío.
            self._level_changed_at = None
        return True

    def _send_next(self):
        with self._condition:
            # Espera un cambio de nivel; si no llega en 1 s, toca el latido.
            self._condition.wait_for(
                lambda: not self._running or self._level != self._sent_level,
                timeout=self._heartbeat_interval,
            )
            if not self._running:
                return
            level = self._level
            changed_at = self._level_changed_at
            is_change = level != self._sent_level

        try:
            if is_change:
                self.bracelet.send_level(level)
            else:
                self.bracelet.send_heartbeat(level)
        except BraceletError as error:
            self._mark_disconnected(error)
            return

        with self._condition:
            self._sent_level = level
            if is_change and changed_at is not None:
                self._latency_ms = (time.monotonic() - changed_at) * 1000

    def _mark_disconnected(self, error):
        with self._condition:
            self._connected = False
            self._last_error = str(error)
        self.bracelet.close()

    def _shutdown(self):
        # Al terminar se apaga el motor, sin esperar los 3 s de RF-09.
        if self._connected:
            try:
                self.bracelet.send_level(0)
            except BraceletError:
                pass
        self.bracelet.close()
        with self._condition:
            self._connected = False
