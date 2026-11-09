"""Pruebas de M4. Cada prueba usa una base SQLite temporal.

Ejecutar desde la carpeta registry_service:
    python -m unittest -v
"""

import tempfile
import unittest
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Aviso informativo de Starlette sobre httpx; no afecta a las pruebas.
warnings.filterwarnings('ignore', message='Using `httpx`')

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app
from app.services import compute_summary

START = datetime(2026, 11, 10, 15, 0, 0, tzinfo=timezone.utc)  # 10:00 en Guayaquil.
SESSION = 'sesion-0001'


def iso(seconds):
    return (START + timedelta(seconds=seconds)).isoformat()


def event(number, seconds, level, degraded_reason=None, ear=0.21, mar=0.05):
    return {'id': f'evento-{number:04d}', 'at': iso(seconds), 'level': level,
            'degraded_reason': degraded_reason, 'ear': ear, 'mar': mar}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db_path = Path(folder.name) / 'registry.sqlite3'
        self.client = self.open_client()

    def open_client(self):
        client = TestClient(create_app(self.db_path))
        client.__enter__()  # Ejecuta el arranque: crea las tablas.
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def create_session(self, session_id=SESSION):
        return self.client.post('/api/v1/sessions', json={
            'id': session_id, 'started_at': iso(0), 'bracelet_mode': 'simulated'})

    def send(self, *events, session_id=SESSION):
        return self.client.post(f'/api/v1/sessions/{session_id}/events',
                                json={'events': list(events)})

    def finish(self, seconds=90, threshold=0.21):
        return self.client.post(f'/api/v1/sessions/{SESSION}/finish',
                                json={'ended_at': iso(seconds), 'threshold': threshold})


class HealthTests(ApiTestCase):
    def test_health(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
        self.assertEqual(response.json()['database'], 'ok')

    def test_docs_are_available(self):
        self.assertEqual(self.client.get('/docs').status_code, 200)
        paths = self.client.get('/openapi.json').json()['paths']
        self.assertIn('/api/v1/sessions/{session_id}/events.csv', paths)


class SessionTests(ApiTestCase):
    def test_create_and_read(self):
        response = self.create_session()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['status'], 'open')
        self.assertEqual(self.client.get(f'/api/v1/sessions/{SESSION}').json()['id'], SESSION)

    def test_creating_twice_does_not_duplicate(self):
        self.create_session()
        response = self.create_session()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.client.get('/api/v1/sessions').json()), 1)

    def test_finish(self):
        self.create_session()
        response = self.finish()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'finished')
        self.assertAlmostEqual(response.json()['threshold'], 0.21)

    def test_unknown_session_is_404(self):
        self.assertEqual(self.client.get('/api/v1/sessions/no-existe-1').status_code, 404)
        self.assertEqual(self.finish().status_code, 404)
        self.assertEqual(self.send(event(1, 1, 1)).status_code, 404)
        self.assertEqual(
            self.client.get('/api/v1/sessions/no-existe-1/summary').json()['detail'],
            'Sesión no encontrada.')

    def test_history_newest_first(self):
        self.create_session('sesion-vieja')
        self.client.post('/api/v1/sessions', json={'id': 'sesion-nueva', 'started_at': iso(600)})
        ids = [session['id'] for session in self.client.get('/api/v1/sessions').json()]
        self.assertEqual(ids, ['sesion-nueva', 'sesion-vieja'])

    def test_data_survives_a_restart(self):
        """La base SQLite es propia de M4: si el servicio se reinicia, los datos siguen."""
        self.create_session()
        self.send(event(1, 5, 1))
        self.client = self.open_client()
        self.assertEqual(self.client.get(f'/api/v1/sessions/{SESSION}').json()['event_count'], 1)


class EventTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.create_session()

    def test_events_are_stored_in_time_order(self):
        result = self.send(event(2, 20, 2), event(1, 10, 1)).json()
        self.assertEqual(result, {'accepted': 2, 'duplicates': 0})
        events = self.client.get(f'/api/v1/sessions/{SESSION}/events').json()
        self.assertEqual([e['level'] for e in events], [1, 2])
        self.assertEqual(events[0]['status'], 'Señal inicial')
        self.assertAlmostEqual(events[0]['ear'], 0.21)

    def test_retry_does_not_duplicate(self):
        self.send(event(1, 10, 1))
        result = self.send(event(1, 10, 1), event(2, 20, 0)).json()
        self.assertEqual(result, {'accepted': 1, 'duplicates': 1})
        self.assertEqual(len(self.client.get(f'/api/v1/sessions/{SESSION}/events').json()), 2)

    def test_rejects_level_4(self):
        """El nivel 4 fue eliminado: sólo existen 0, 1, 2 y 3."""
        self.assertEqual(self.send(event(1, 10, 4)).status_code, 422)

    def test_accepts_unusually_high_metrics(self):
        """Con la cabeza muy girada el ojo se ve angosto y el EAR puede ser muy alto."""
        result = self.send(event(1, 10, 0, ear=3.7, mar=6.2))
        self.assertEqual(result.json(), {'accepted': 1, 'duplicates': 0})

    def test_rejects_images(self):
        """RNF-05: no existe ningún campo para imágenes; si llega uno, se rechaza."""
        bad = {**event(1, 10, 1), 'image': 'data:image/jpeg;base64,/9j/4AAQ'}
        self.assertEqual(self.send(bad).status_code, 422)
        self.assertEqual(len(self.client.get(f'/api/v1/sessions/{SESSION}/events').json()), 0)

    def test_rejects_dates_without_time_zone(self):
        bad = {**event(1, 10, 1), 'at': '2026-11-10T10:00:00'}
        self.assertEqual(self.send(bad).status_code, 422)

    def test_degraded_event_status(self):
        self.send(event(1, 10, 0, degraded_reason='low_light', ear=None, mar=None))
        events = self.client.get(f'/api/v1/sessions/{SESSION}/events').json()
        self.assertEqual(events[0]['status'], 'Medición no disponible (Poca luz)')
        self.assertIsNone(events[0]['ear'])


class SummaryTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.create_session()
        self.send(event(1, 10, 1), event(2, 12, 2), event(3, 14, 3), event(4, 16, 0),
                  event(5, 40, 0, degraded_reason='no_face', ear=None, mar=None),
                  event(6, 41, 0), event(7, 60, 1), event(8, 62, 0))

    def test_summary(self):
        self.finish(seconds=90)
        summary = self.client.get(f'/api/v1/sessions/{SESSION}/summary').json()
        self.assertEqual(summary['alert_count'], 2)  # 0→1 a los 10 s y 0→1 a los 60 s.
        self.assertEqual(summary['max_level'], 3)
        self.assertEqual(summary['max_level_name'], 'Riesgo alto')
        self.assertEqual(summary['duration_s'], 90.0)
        self.assertEqual(summary['event_count'], 8)
        self.assertTrue(summary['finished'])

    def test_open_session_duration_counts_until_now(self):
        started = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.client.post('/api/v1/sessions', json={'id': 'sesion-abierta',
                                                   'started_at': started.isoformat()})
        summary = self.client.get('/api/v1/sessions/sesion-abierta/summary').json()
        self.assertFalse(summary['finished'])
        self.assertGreaterEqual(summary['duration_s'], 120)

    def test_csv(self):
        response = self.client.get(f'/api/v1/sessions/{SESSION}/events.csv')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('text/csv'))
        self.assertIn('filename="vigia_sesion_20261110_1000.csv"',
                      response.headers['content-disposition'])
        lines = response.content.decode('utf-8').splitlines()
        self.assertEqual(lines[0], '﻿hora,nivel,estado,ear,mar')
        self.assertEqual(len(lines), 9)  # Encabezado + 8 eventos.
        self.assertEqual(lines[1], '2026-11-10 10:00:10,1,Señal inicial,0.210,0.050')
        self.assertEqual(lines[5], '2026-11-10 10:00:40,0,Medición no disponible (Rostro no detectado),,')


class ComputeSummaryTests(unittest.TestCase):
    def test_without_events(self):
        session = {'id': 's', 'started_at': START, 'ended_at': START + timedelta(seconds=5),
                   'threshold': None}
        summary = compute_summary(session, [])
        self.assertEqual((summary.alert_count, summary.max_level, summary.duration_s), (0, 0, 5.0))

    def test_rising_level_is_one_alert(self):
        session = {'id': 's', 'started_at': START, 'ended_at': None, 'threshold': 0.2}
        events = [{'level': level} for level in (1, 2, 3, 2, 0)]
        summary = compute_summary(session, events, now=START + timedelta(seconds=30))
        self.assertEqual((summary.alert_count, summary.max_level, summary.duration_s), (1, 3, 30.0))


if __name__ == '__main__':
    unittest.main()
