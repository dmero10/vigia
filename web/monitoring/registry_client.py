"""Infraestructura: comunicación con el microservicio de registro M4 (HTTP/JSON).

- `RegistryClient`: hace cada petición a la API de M4 (sólo biblioteca estándar).
- `RegistryQueue`: cola de envío en su propio hilo, con confirmaciones y reintentos.
  Encolar nunca espera a la red, así que el registro jamás retrasa la alerta a la
  pulsera. Si M4 está apagado, los datos esperan en la cola (en memoria) y se
  envían en orden cuando vuelve. Los identificadores los genera Django, por eso un
  reenvío nunca se guarda dos veces en M4.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

MAX_BATCH = 50  # Eventos por petición.
MAX_PENDING = 10_000  # Límite de la cola en memoria.
RETRY_MIN_S = 0.5
RETRY_MAX_S = 5.0

OP_SESSION = 'session'
OP_EVENT = 'event'
OP_FINISH = 'finish'


class RegistryError(Exception):
    """Base de los errores de M4."""


class RegistryUnavailable(RegistryError):
    """M4 no responde (apagado, tiempo agotado o error 5xx): se reintenta."""


class RegistryRejected(RegistryError):
    """M4 rechazó el dato (error 4xx): reenviarlo no serviría."""

    def __init__(self, status, detail):
        super().__init__(f'M4 rechazó la petición ({status}): {detail}')
        self.status = status


class RegistryClient:
    def __init__(self, base_url, timeout=2.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def health(self):
        return self._json('GET', '/health')

    def create_session(self, payload):
        return self._json('POST', '/api/v1/sessions', payload)

    def add_events(self, session_id, events):
        return self._json('POST', f'/api/v1/sessions/{session_id}/events', {'events': events})

    def finish_session(self, session_id, payload):
        return self._json('POST', f'/api/v1/sessions/{session_id}/finish', payload)

    def summary(self, session_id):
        return self._json('GET', f'/api/v1/sessions/{session_id}/summary')

    def events(self, session_id):
        return self._json('GET', f'/api/v1/sessions/{session_id}/events')

    def sessions(self, limit=50):
        return self._json('GET', f'/api/v1/sessions?limit={limit}')

    def events_csv(self, session_id):
        return self._request('GET', f'/api/v1/sessions/{session_id}/events.csv')

    def _json(self, method, path, payload=None):
        return json.loads(self._request(method, path, payload))

    def _request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode('utf-8', 'replace')[:300]
            if error.code >= 500:
                raise RegistryUnavailable(f'M4 respondió {error.code}.') from error
            raise RegistryRejected(error.code, detail) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            reason = getattr(error, 'reason', error)
            raise RegistryUnavailable(f'No se pudo conectar con M4 ({reason}).') from error


@dataclass(frozen=True)
class RegistryStatus:
    reachable: bool | None  # None = todavía no se intentó.
    pending: int
    sent: int
    dropped: int
    last_error: str | None


class RegistryQueue:
    """Cola de envío a M4. Las operaciones se envían en el orden en que se encolan."""

    def __init__(self, client, retry_min_s=RETRY_MIN_S, retry_max_s=RETRY_MAX_S):
        self.client = client
        self._retry_min_s = retry_min_s
        self._retry_max_s = retry_max_s
        self._items = deque()
        self._condition = threading.Condition()
        self._wake = threading.Event()
        self._thread = None
        self._in_flight = 0
        self._reachable = None
        self._sent = 0
        self._dropped = 0
        self._last_error = None

    # ---------- Lo que usa el orquestador (nunca espera a la red) ----------

    def start_session(self, session_id, started_at, bracelet_mode=None):
        self._put(OP_SESSION, session_id, {
            'id': session_id, 'started_at': started_at.isoformat(),
            'bracelet_mode': bracelet_mode,
        })

    def add_event(self, session_id, event):
        payload = dict(event)
        payload['at'] = payload['at'].isoformat()
        self._put(OP_EVENT, session_id, payload)

    def finish_session(self, session_id, ended_at, threshold=None):
        self._put(OP_FINISH, session_id, {'ended_at': ended_at.isoformat(), 'threshold': threshold})

    # ---------- Lo que usan las vistas ----------

    def flush(self, timeout):
        """Pide un reintento inmediato y espera hasta `timeout` s a que la cola se vacíe.
        Devuelve True si todo quedó confirmado por M4."""
        self._wake.set()
        end = time.monotonic() + timeout
        with self._condition:
            while self._items or self._in_flight:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def status(self):
        with self._condition:
            return RegistryStatus(
                reachable=self._reachable,
                pending=len(self._items) + self._in_flight,
                sent=self._sent,
                dropped=self._dropped,
                last_error=self._last_error,
            )

    # ---------- Hilo de envío ----------

    def _put(self, kind, session_id, payload):
        with self._condition:
            if len(self._items) >= MAX_PENDING:
                self._dropped += 1
                logger.warning('Cola de M4 llena: se descarta un dato.')
                return
            self._items.append((kind, session_id, payload))
            self._condition.notify_all()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='registry', daemon=True)
                self._thread.start()

    def _take(self):
        """Saca de la cola la siguiente operación (o un lote de eventos de una sesión)."""
        with self._condition:
            while not self._items:
                self._condition.wait()
            kind, session_id, payload = self._items.popleft()
            batch = [payload]
            if kind == OP_EVENT:
                while (self._items and len(batch) < MAX_BATCH
                       and self._items[0][0] == OP_EVENT and self._items[0][1] == session_id):
                    batch.append(self._items.popleft()[2])
            self._in_flight = len(batch)
            return kind, session_id, batch

    def _give_back(self, kind, session_id, batch):
        """El envío falló: el lote vuelve al principio para mantener el orden."""
        with self._condition:
            for payload in reversed(batch):
                self._items.appendleft((kind, session_id, payload))
            self._in_flight = 0
            self._condition.notify_all()

    def _done(self, count, sent):
        with self._condition:
            if sent:
                self._sent += count
            else:
                self._dropped += count
            self._in_flight = 0
            self._condition.notify_all()

    def _run(self):
        delay = self._retry_min_s
        while True:
            kind, session_id, batch = self._take()
            try:
                self._send(kind, session_id, batch)
            except RegistryUnavailable as error:
                self._give_back(kind, session_id, batch)
                self._set_error(False, str(error))
                self._wake.wait(delay)  # «flush» lo despierta para reintentar ya.
                self._wake.clear()
                delay = min(delay * 2, self._retry_max_s)
                continue
            except RegistryRejected as error:
                logger.error('%s Se descartan %d dato(s).', error, len(batch))
                self._done(len(batch), sent=False)
                self._set_error(True, str(error))
            except Exception as error:  # noqa: BLE001 - el hilo de envío nunca debe morir
                logger.exception('Error inesperado al enviar a M4')
                self._done(len(batch), sent=False)
                self._set_error(None, f'Error inesperado: {error}')
            else:
                self._done(len(batch), sent=True)
                self._set_error(True, None)
            delay = self._retry_min_s

    def _send(self, kind, session_id, batch):
        if kind == OP_SESSION:
            self.client.create_session(batch[0])
        elif kind == OP_EVENT:
            self.client.add_events(session_id, batch)
        else:
            self.client.finish_session(session_id, batch[0])

    def _set_error(self, reachable, message):
        with self._condition:
            self._reachable = reachable
            self._last_error = message


class NullRegistry:
    """Sin registro (pruebas del orquestador). Acepta todo y no envía nada."""

    def start_session(self, *args, **kwargs):
        pass

    def add_event(self, *args, **kwargs):
        pass

    def finish_session(self, *args, **kwargs):
        pass


_queue = None
_queue_lock = threading.Lock()


def get_registry_queue():
    """La cola única del proceso, conectada a `settings.REGISTRY_URL`."""
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = RegistryQueue(RegistryClient(settings.REGISTRY_URL))
        return _queue
