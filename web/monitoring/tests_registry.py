"""Pruebas del cliente de M4: cola de envío (sin red) y cliente HTTP (servidor local)."""

import http.server
import json
import threading
import time

from django.test import SimpleTestCase
from django.utils import timezone

from monitoring.registry_client import (
    RegistryClient,
    RegistryQueue,
    RegistryRejected,
    RegistryUnavailable,
)


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


class FakeClient:
    """Cliente falso: puede simular M4 apagado o datos rechazados."""

    def __init__(self):
        self.down = False
        self.reject = False
        self.received = []
        self.attempts = 0

    def _call(self, kind, session_id, payload):
        self.attempts += 1
        if self.down:
            raise RegistryUnavailable('M4 apagado')
        if self.reject:
            raise RegistryRejected(422, 'dato inválido')
        self.received.append((kind, session_id, payload))

    def create_session(self, payload):
        self._call('session', payload['id'], payload)

    def add_events(self, session_id, events):
        self._call('events', session_id, events)

    def finish_session(self, session_id, payload):
        self._call('finish', session_id, payload)


class RegistryQueueTests(SimpleTestCase):
    def setUp(self):
        self.fake = FakeClient()
        self.queue = RegistryQueue(self.fake, retry_min_s=0.01, retry_max_s=0.05)
        self.now = timezone.now()

    def fill(self, events=3):
        self.queue.start_session('sesion-1', self.now, 'simulated')
        for number in range(events):
            self.queue.add_event('sesion-1', {'id': f'e{number}', 'at': self.now, 'level': 1})
        self.queue.finish_session('sesion-1', self.now, 0.2)

    def test_sends_in_order(self):
        self.fill()
        self.assertTrue(self.queue.flush(2))
        kinds = [item[0] for item in self.fake.received]
        self.assertEqual(kinds[0], 'session')
        self.assertEqual(kinds[-1], 'finish')
        self.assertTrue(all(kind == 'events' for kind in kinds[1:-1]))
        sent = [event for item in self.fake.received[1:-1] for event in item[2]]
        self.assertEqual([event['id'] for event in sent], ['e0', 'e1', 'e2'])
        self.assertIsInstance(sent[0]['at'], str)  # Fechas en ISO para el JSON.
        status = self.queue.status()
        self.assertEqual((status.pending, status.sent, status.reachable), (0, 5, True))

    def test_enqueue_never_waits_while_m4_is_down(self):
        """El orquestador encola y sigue: la alerta a la pulsera no se retrasa."""
        self.fake.down = True
        started = time.monotonic()
        self.fill(events=200)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(self.queue.flush(0.1))
        status = self.queue.status()
        self.assertEqual(status.pending, 202)
        self.assertFalse(status.reachable)
        self.assertIn('M4 apagado', status.last_error)

    def test_everything_arrives_in_order_when_m4_comes_back(self):
        self.fake.down = True
        self.fill()
        self.assertTrue(wait_until(lambda: self.fake.attempts >= 3))  # Reintenta.
        self.fake.down = False
        self.assertTrue(self.queue.flush(2))
        # Lo acumulado se envía en orden y los 3 eventos van en un solo lote.
        self.assertEqual([item[0] for item in self.fake.received], ['session', 'events', 'finish'])
        self.assertEqual(len(self.fake.received[1][2]), 3)
        self.assertEqual(self.queue.status().sent, 5)
        self.assertTrue(self.queue.status().reachable)

    def test_rejected_data_is_dropped_not_retried_forever(self):
        self.fake.reject = True
        with self.assertLogs('monitoring.registry_client', level='ERROR'):
            self.fill(events=1)
            self.assertTrue(self.queue.flush(2))
        self.assertEqual(self.queue.status().dropped, 3)


class _Handler(http.server.BaseHTTPRequestHandler):
    received = None  # Lista compartida, la asigna cada prueba.

    def do_GET(self):
        if self.path == '/health':
            self._reply(200, {'status': 'ok'})
        else:
            self._reply(404, {'detail': 'Sesión no encontrada.'})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.received.append(body)
        ok = bool(body.get('ok', body.get('events')))
        self._reply(200 if ok else 500, {'accepted': 1, 'duplicates': 0})

    def _reply(self, code, data):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class RegistryClientTests(SimpleTestCase):
    """El cliente HTTP real contra un servidor local mínimo."""

    def setUp(self):
        _Handler.received = []
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = RegistryClient(f'http://127.0.0.1:{self.server.server_port}', timeout=1)

    def test_json_request_and_response(self):
        self.assertEqual(self.client.health(), {'status': 'ok'})
        result = self.client.add_events('sesion-1', [{'id': 'e1'}])
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(_Handler.received[-1], {'events': [{'id': 'e1'}]})

    def test_4xx_is_rejected_and_5xx_is_unavailable(self):
        with self.assertRaises(RegistryRejected) as context:
            self.client.summary('sesion-x')
        self.assertEqual(context.exception.status, 404)
        with self.assertRaises(RegistryUnavailable):
            self.client.create_session({'ok': False})

    def test_service_down_is_unavailable(self):
        port = self.server.server_port
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(RegistryUnavailable):
            RegistryClient(f'http://127.0.0.1:{port}', timeout=0.5).health()
