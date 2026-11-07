"""M3 - Pulsera: interfaz común y pulsera simulada.

La pulsera real (BLE con Bleak) irá en `ble_client.py` cuando tengamos los UUID
y el formato de los mensajes. Debe heredar de `Bracelet`, para que el resto del
sistema funcione igual con cualquiera de las dos.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass

SIGNAL_TIMEOUT_S = 3.0  # RF-09: sin señal durante este tiempo, el motor se apaga.


class BraceletError(Exception):
    """La pulsera no está disponible o se desconectó."""


class Bracelet:
    """Lo que toda pulsera debe saber hacer. Las llamadas pueden bloquear."""

    name = 'Pulsera'

    @property
    def is_connected(self):
        raise NotImplementedError

    def connect(self):
        """Conecta con la pulsera o lanza `BraceletError`."""
        raise NotImplementedError

    def send_level(self, level):
        """Envía un nivel nuevo (0-3) o lanza `BraceletError`."""
        raise NotImplementedError

    def send_heartbeat(self, level):
        """Latido de cada segundo. Por defecto reenvía el nivel actual."""
        self.send_level(level)

    def close(self):
        raise NotImplementedError


@dataclass(frozen=True)
class ReceivedMessage:
    at: float  # Segundos de reloj monótono.
    kind: str  # 'level' o 'heartbeat'.
    level: int


class SimulatedBracelet(Bracelet):
    """Pulsera de mentira para la demo y las pruebas. Imita el apagado de RF-09."""

    name = 'Pulsera simulada'

    def __init__(self, clock=time.monotonic, history=200):
        self._clock = clock
        self._lock = threading.Lock()
        self._available = True
        self._connected = False
        self._level = 0
        self._last_signal = None
        self._received = deque(maxlen=history)

    @property
    def is_connected(self):
        with self._lock:
            return self._connected

    def connect(self):
        with self._lock:
            if not self._available:
                raise BraceletError('La pulsera simulada no está disponible.')
            self._connected = True

    def send_level(self, level):
        self._receive('level', level)

    def send_heartbeat(self, level):
        self._receive('heartbeat', level)

    def close(self):
        with self._lock:
            self._connected = False

    def set_available(self, available):
        """Simula apagar o alejar la pulsera (pantalla «Pulsera desconectada»)."""
        with self._lock:
            self._available = available
            if not available:
                self._connected = False

    def messages(self):
        """Copia de los últimos mensajes recibidos."""
        with self._lock:
            return list(self._received)

    @property
    def vibrating_level(self):
        """Nivel con el que vibraría el motor; 0 si pasaron 3 s sin señal (RF-09)."""
        with self._lock:
            if self._last_signal is None or self._clock() - self._last_signal > SIGNAL_TIMEOUT_S:
                return 0
            return self._level

    def _receive(self, kind, level):
        with self._lock:
            if not self._connected:
                raise BraceletError('La pulsera simulada está desconectada.')
            self._level = level
            self._last_signal = self._clock()
            self._received.append(ReceivedMessage(self._last_signal, kind, level))
