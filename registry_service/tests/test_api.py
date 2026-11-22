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


def event(number, seconds, level, degraded_reason=None, ear=0.21, mar=0.05, closed_ms=0):
    return {'id': f'evento-{number:04d}', 'at': iso(seconds), 'level': level,
            'degraded_reason': degraded_reason, 'ear': ear, 'mar': mar, 'closed_ms': closed_ms}


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
        # Alerta 1: ojos cerrados (10 s a 16 s). Alerta 2: bostezos (60 s a 63 s).
        self.send(event(1, 10, 1, closed_ms=1500), event(2, 12, 2, closed_ms=3000),
                  event(3, 14, 3, closed_ms=5000), event(4, 16, 0),
                  event(5, 40, 0, degraded_reason='no_face', ear=None, mar=None),
                  event(6, 41, 0), event(7, 60, 1), event(8, 63, 0))

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

    def test_events_carry_their_explanation(self):
        events = self.client.get(f'/api/v1/sessions/{SESSION}/events').json()
        self.assertEqual(events[0]['event'], 'Empieza una alerta (nivel 1)')
        self.assertEqual(events[7]['event'], 'Termina la alerta')
        self.assertEqual(events[7]['detail'], 'Pasó el aviso por bostezos. La alerta duró 3.0 s')

    def csv_lines(self, session_id=SESSION):
        response = self.client.get(f'/api/v1/sessions/{session_id}/events.csv')
        self.assertEqual(response.status_code, 200)
        return response, response.content.decode('utf-8').splitlines()

    def section(self, lines, title):
        """Filas de un bloque del CSV: desde su título hasta la línea en blanco."""
        first = next(i for i, line in enumerate(lines) if line.startswith(title))
        rows = []
        for line in lines[first + 1:]:
            if not line:
                break
            rows.append(line.split(';'))
        return rows

    def test_csv(self):
        self.client.post(f'/api/v1/sessions/{SESSION}/finish', json={
            'ended_at': iso(90), 'threshold': 0.21,
            'monitoring_s': 80, 'fps_avg': 30.0, 'fps_min': 27.0})
        response, lines = self.csv_lines()
        self.assertTrue(response.headers['content-type'].startswith('text/csv'))
        self.assertIn('filename="vigia_sesion_20261110_1000.csv"',
                      response.headers['content-disposition'])
        # BOM (tildes en Excel) y título.
        self.assertEqual(lines[0], '\ufeffVIGÍA · Registro de la sesión de monitoreo')

        summary = dict(row[:2] for row in self.section(lines, 'RESUMEN'))
        self.assertEqual(summary['Fecha'], '2026-11-10')
        self.assertEqual(summary['Hora de inicio'], '10:00:00')  # Hora de Guayaquil.
        self.assertEqual(summary['Hora de fin'], '10:01:30')
        self.assertEqual(summary['Duración total'], '00:01:30')
        self.assertEqual(summary['Pulsera'], 'Simulada')
        self.assertEqual(summary['Número de alertas'], '2')
        self.assertEqual(summary['Nivel máximo'], '3 · Riesgo alto')
        self.assertEqual(summary['Umbral de ojos cerrados (EAR)'], '0.210')
        self.assertEqual(summary['FPS mínimo'], '27.0')
        self.assertEqual(summary['Prueba de 5 min fluidos (RNF-02)'],
                         'No cumplida: 00:01:20 de monitoreo (se piden 5 min)')

        alerts = self.section(lines, 'ALERTAS DE LA SESIÓN')
        self.assertEqual(alerts[0], ['Alerta N.º', 'Hora de inicio', 'Duración',
                                     'Nivel máximo', 'Motivo', 'Cómo terminó'])
        self.assertEqual(alerts[1], ['1', '10:00:10', '6.0 s', '3 · Riesgo alto',
                                     'Ojos cerrados', 'Abrió los ojos'])
        self.assertEqual(alerts[2], ['2', '10:01:00', '3.0 s', '1 · Señal inicial',
                                     'Bostezos frecuentes', 'Pasó el aviso por bostezos'])
        self.assertEqual(len(alerts), 3)  # Encabezado + 2 alertas.

        log = self.section(lines, 'REGISTRO DETALLADO')
        self.assertEqual(log[0], ['Hora', 'Nivel', 'Estado', 'Alerta N.º', 'Qué pasó',
                                  'Detalle', 'EAR (ojos)', 'MAR (boca)'])
        self.assertEqual(len(log), 9)  # Encabezado + 8 eventos.
        self.assertEqual(log[1], ['10:00:10', '1', 'Señal inicial', '1',
                                  'Empieza una alerta (nivel 1)',
                                  'Ojos cerrados 1.5 s seguidos', '0.210', '0.050'])
        self.assertEqual(log[2][4:6], ['Sube a nivel 2', 'Ojos cerrados 3.0 s seguidos'])
        self.assertEqual(log[4][3:6], ['1', 'Termina la alerta',
                                       'Abrió los ojos. La alerta duró 6.0 s'])
        self.assertEqual(log[5], ['10:00:40', '0', 'Medición no disponible', '',
                                  'Medición no disponible', 'No se ve el rostro', '', ''])
        self.assertEqual(log[6][4], 'Medición recuperada')
        self.assertEqual(log[7][3:6], ['2', 'Empieza una alerta (nivel 1)',
                                       'Bostezos frecuentes: 3 en menos de 5 minutos'])

        guide = dict(row[:2] for row in self.section(lines, 'CÓMO LEERLO'))
        self.assertIn('EAR (ojos)', guide)
        self.assertIn('nunca imágenes', guide['Privacidad'])

    def test_csv_of_open_session_with_active_alert(self):
        self.client.post('/api/v1/sessions', json={'id': 'sesion-abierta',
                                                   'started_at': iso(0)})
        self.send(event(20, 5, 2, closed_ms=3000), session_id='sesion-abierta')
        _, lines = self.csv_lines('sesion-abierta')
        summary = dict(row[:2] for row in self.section(lines, 'RESUMEN'))
        self.assertEqual(summary['Hora de fin'], 'Sesión abierta')
        self.assertEqual(summary['Prueba de 5 min fluidos (RNF-02)'], 'Sin datos de FPS')
        self.assertEqual(self.section(lines, 'ALERTAS DE LA SESIÓN')[1][5], 'Sigue activa')

    def test_csv_without_alerts(self):
        self.client.post('/api/v1/sessions', json={'id': 'sesion-vacia',
                                                   'started_at': iso(0)})
        _, lines = self.csv_lines('sesion-vacia')
        self.assertEqual(self.section(lines, 'ALERTAS DE LA SESIÓN'),
                         [['No hubo alertas en esta sesión']])
        summary = dict(row[:2] for row in self.section(lines, 'RESUMEN'))
        self.assertEqual(summary['Umbral de ojos cerrados (EAR)'], 'Sin calibrar')


class Rnf02Tests(ApiTestCase):
    """RNF-02: evidencia de 5 minutos de monitoreo sin bajar de 25 FPS."""

    def setUp(self):
        super().setUp()
        self.create_session()

    def finish_with(self, monitoring_s, fps_min):
        return self.client.post(f'/api/v1/sessions/{SESSION}/finish', json={
            'ended_at': iso(400), 'threshold': 0.2,
            'monitoring_s': monitoring_s, 'fps_avg': 29.6, 'fps_min': fps_min})

    def summary(self):
        return self.client.get(f'/api/v1/sessions/{SESSION}/summary').json()

    def test_passed(self):
        self.assertEqual(self.finish_with(312.5, 27.0).status_code, 200)
        summary = self.summary()
        self.assertTrue(summary['rnf02_passed'])
        self.assertEqual((summary['monitoring_s'], summary['fps_avg'], summary['fps_min']),
                         (312.5, 29.6, 27.0))

    def test_too_short_or_too_slow(self):
        self.finish_with(120.0, 29.0)
        self.assertFalse(self.summary()['rnf02_passed'])
        self.finish_with(310.0, 22.5)
        self.assertFalse(self.summary()['rnf02_passed'])

    def test_unknown_without_stats(self):
        """Un cliente con el contrato 1.0 no manda FPS: no se puede decir si cumplió."""
        self.finish()
        self.assertIsNone(self.summary()['rnf02_passed'])

    def test_old_database_gets_the_new_columns(self):
        import sqlite3
        old_db = self.db_path.with_name('vieja.sqlite3')
        with sqlite3.connect(old_db) as connection:
            connection.executescript(
                'CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, '
                'ended_at TEXT, bracelet_mode TEXT, threshold REAL, created_at TEXT NOT NULL);'
                "INSERT INTO sessions VALUES ('sesion-vieja', '2026-11-01T10:00:00.000000+00:00',"
                " NULL, NULL, NULL, '2026-11-01T10:00:00.000000+00:00');")
        connection.close()
        self.db_path = old_db
        client = self.open_client()
        response = client.get('/api/v1/sessions/sesion-vieja/summary')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['fps_min'])


class ExplainTests(unittest.TestCase):
    """El registro en palabras: cada evento dice qué pasó y por qué."""

    def ev(self, seconds, level, reason=None, closed_ms=0):
        return {'at': START + timedelta(seconds=seconds), 'level': level,
                'degraded_reason': reason, 'closed_ms': closed_ms}

    def test_alerts_are_grouped_from_0_to_0(self):
        from app.explain import explain
        log, alerts = explain([self.ev(10, 1, closed_ms=1500), self.ev(12, 2, closed_ms=3000),
                               self.ev(14.5, 0)])
        self.assertEqual([row['event'] for row in log],
                         ['Empieza una alerta (nivel 1)', 'Sube a nivel 2', 'Termina la alerta'])
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]['max_level'], alerts[0]['cause'], alerts[0]['duration_s']),
                         (2, 'Ojos cerrados', 4.5))
        self.assertEqual(alerts[0]['ended_by'], 'Abrió los ojos')
        self.assertEqual(alerts[0]['max_level_name'], 'Atención reducida')

    def test_degraded_measurement_cuts_the_alert(self):
        from app.explain import explain
        log, alerts = explain([self.ev(10, 1, closed_ms=1500),
                               self.ev(11, 0, 'low_light'), self.ev(20, 0)])
        self.assertEqual(log[1]['event'], 'Medición no disponible')
        self.assertEqual(log[1]['detail'], 'Hay poca luz. La alerta se corta tras 1.0 s')
        self.assertTrue(log[1]['degraded'])
        self.assertEqual(log[2]['event'], 'Medición recuperada')
        self.assertEqual(alerts[0]['ended_by'], 'Se cortó: medición no disponible')

    def test_alert_still_active_at_the_end(self):
        from app.explain import explain
        _, alerts = explain([self.ev(10, 3, closed_ms=5000)], ended_at=START + timedelta(seconds=25))
        self.assertEqual(alerts[0]['ended_by'], 'Seguía activa al finalizar')
        self.assertEqual(alerts[0]['duration_s'], 15.0)

    def test_format_seconds(self):
        from app.explain import format_seconds
        self.assertEqual(format_seconds(4.21), '4.2 s')
        self.assertEqual(format_seconds(15.4), '15 s')
        self.assertEqual(format_seconds(65), '1 min 05 s')


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
