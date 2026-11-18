"""Pruebas de monitoring con cámara, rostro y pulsera simulados."""

import json
import time
from types import SimpleNamespace
from unittest import mock

import numpy as np
from django.core.serializers.json import DjangoJSONEncoder
from django.test import SimpleTestCase
from django.urls import reverse

from acquisition.camera import CameraError
from alerts.bracelet import SimulatedBracelet
from analysis.metrics import LEFT_EYE, RIGHT_EYE
from monitoring.orchestrator import (
    PHASE_CALIBRATED,
    PHASE_CALIBRATING,
    PHASE_ERROR,
    PHASE_FINISHED,
    PHASE_MONITORING,
    PHASE_PREVIEW,
    Orchestrator,
)
from monitoring.registry_client import RegistryStatus, RegistryUnavailable


def wait_until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


def face(eye_half_height):
    """Rostro sintético: EAR = 2 * mitad de la altura del ojo / 10."""
    points = np.zeros((478, 2))
    for eye in (RIGHT_EYE, LEFT_EYE):
        p1, p2, p3, p4, p5, p6 = eye
        points[p1], points[p4] = (0, 0), (10, 0)
        points[p2], points[p6] = (3, eye_half_height), (3, -eye_half_height)
        points[p3], points[p5] = (7, eye_half_height), (7, -eye_half_height)
    points[61], points[291] = (0, 0), (10, 0)  # Boca cerrada: MAR = 0.
    return points


OPEN_FACE = face(2)  # EAR 0.4 -> umbral calibrado 0.30
CLOSED_FACE = face(0.2)  # EAR 0.04


class FakeCamera:
    """Cámara con reloj simulado: cada lectura avanza 33 ms."""

    def __init__(self, brightness=100, fail=False):
        self.brightness = brightness
        self.fail = fail
        self.timestamp_ms = 0
        self.closed = False

    def __enter__(self):
        if self.fail:
            raise CameraError('No se pudo abrir la cámara 0.')
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self):
        time.sleep(0.0005)  # Evita que el hilo acapare la CPU durante las pruebas.
        self.timestamp_ms += 33
        image = np.full((48, 64, 3), self.brightness, dtype=np.uint8)
        return SimpleNamespace(image=image, timestamp_ms=self.timestamp_ms)


class FakeDetector:
    """Devuelve el rostro que diga `face_for()`."""

    def __init__(self, face_for):
        self.face_for = face_for

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def detect(self, image, timestamp_ms):
        return self.face_for()


class OrchestratorTestCase(SimpleTestCase):
    def setUp(self):
        self.face = OPEN_FACE
        self.camera = FakeCamera()
        self.bracelet = SimulatedBracelet()
        self.registry = RecordingRegistry()

    def make(self):
        orchestrator = Orchestrator(
            camera_factory=lambda: self.camera,
            detector_factory=lambda: FakeDetector(self.current_face),
            bracelet_factory=lambda mode: self.bracelet,
            registry=self.registry,
        )
        self.addCleanup(orchestrator.stop)
        return orchestrator

    def current_face(self):
        face_value = self.face
        if callable(face_value):
            return face_value()
        return face_value

    def wait_phase(self, orchestrator, phase):
        self.assertTrue(
            wait_until(lambda: orchestrator.state().phase == phase),
            f'No llegó a {phase}; estado: {orchestrator.state()}',
        )

    def run_to_monitoring(self, orchestrator):
        orchestrator.start()
        self.wait_phase(orchestrator, PHASE_PREVIEW)
        orchestrator.send_command('calibrate')
        self.wait_phase(orchestrator, PHASE_CALIBRATED)
        orchestrator.send_command('monitor')
        self.wait_phase(orchestrator, PHASE_MONITORING)


class OrchestratorFlowTests(OrchestratorTestCase):
    def test_waits_in_preview_until_calibrate(self):
        orchestrator = self.make()
        orchestrator.start()
        self.wait_phase(orchestrator, PHASE_PREVIEW)
        time.sleep(0.1)
        state = orchestrator.state()
        self.assertEqual(state.phase, PHASE_PREVIEW)
        self.assertTrue(state.face_detected)
        self.assertTrue(state.light_ok)

    def test_full_flow(self):
        orchestrator = self.make()
        self.run_to_monitoring(orchestrator)

        state = orchestrator.state()
        self.assertAlmostEqual(state.threshold, 0.30)
        self.assertEqual(state.calibration_progress, 1.0)
        self.assertEqual(state.level, 0)
        self.assertAlmostEqual(state.ear, 0.4)
        self.assertTrue(wait_until(lambda: orchestrator.state().bracelet.connected))
        self.assertEqual(orchestrator.state().bracelet_name, 'Pulsera simulada')
        self.assertTrue(orchestrator.state().bracelet_simulated)

        orchestrator.stop()
        self.assertEqual(orchestrator.state().phase, PHASE_FINISHED)
        self.assertFalse(orchestrator.is_running)
        self.assertTrue(self.camera.closed)
        self.assertFalse(self.bracelet.is_connected)
        self.assertEqual(self.bracelet.messages()[-1].level, 0)

        summary = orchestrator.summary()
        self.assertEqual(summary.alert_count, 0)
        self.assertEqual(summary.max_level, 0)
        self.assertAlmostEqual(summary.threshold, 0.30)
        self.assertGreater(summary.duration_s, 0)

    def test_waits_after_calibration_and_can_recalibrate(self):
        orchestrator = self.make()
        orchestrator.start()
        self.wait_phase(orchestrator, PHASE_PREVIEW)
        orchestrator.send_command('calibrate')
        self.wait_phase(orchestrator, PHASE_CALIBRATED)
        time.sleep(0.1)
        self.assertEqual(orchestrator.state().phase, PHASE_CALIBRATED)

        self.face = face(1.5)  # EAR 0.3 -> nuevo umbral 0.225
        orchestrator.send_command('recalibrate')
        self.assertTrue(wait_until(
            lambda: orchestrator.state().phase == PHASE_CALIBRATED
            and abs(orchestrator.state().threshold - 0.225) < 1e-9
        ))

    def test_closed_eyes_raise_alert_and_reach_the_bracelet(self):
        orchestrator = self.make()
        self.run_to_monitoring(orchestrator)
        self.face = CLOSED_FACE
        self.assertTrue(wait_until(lambda: orchestrator.state().level == 3, timeout=10))
        self.face = OPEN_FACE
        self.assertTrue(wait_until(lambda: orchestrator.state().level == 0))
        orchestrator.stop()

        summary = orchestrator.summary()
        self.assertEqual(summary.alert_count, 1)
        self.assertEqual(summary.max_level, 3)
        self.assertEqual([event.level for event in summary.events], [1, 2, 3, 0])
        self.assertIsNotNone(summary.events[0].at.tzinfo)
        # Si los niveles cambian muy rápido, la pulsera recibe sólo el más reciente;
        # por eso no se exige que lleguen el 1 y el 2.
        sent = [message.level for message in self.bracelet.messages() if message.kind == 'level']
        self.assertIn(3, sent)
        self.assertEqual(sent[-1], 0)

    def test_low_light_is_reported_as_degraded(self):
        orchestrator = self.make()
        self.run_to_monitoring(orchestrator)
        self.camera.brightness = 10
        self.assertTrue(wait_until(lambda: orchestrator.state().degraded_reason == 'low_light'))
        self.assertFalse(orchestrator.state().light_ok)

    def test_preview_is_jpeg_only_while_running(self):
        orchestrator = self.make()
        self.assertIsNone(orchestrator.latest_jpeg())
        orchestrator.start()
        self.assertTrue(wait_until(lambda: orchestrator.latest_jpeg() is not None))
        self.assertTrue(orchestrator.latest_jpeg().startswith(b'\xff\xd8'))
        orchestrator.stop()
        self.assertIsNone(orchestrator.latest_jpeg())

    def test_state_can_be_sent_as_json(self):
        orchestrator = self.make()
        orchestrator.start()
        self.assertTrue(wait_until(lambda: orchestrator.state().bracelet is not None))
        data = json.loads(json.dumps(orchestrator.state().as_dict(), cls=DjangoJSONEncoder))
        self.assertIn('connected', data['bracelet'])

    def test_elapsed_time_advances(self):
        orchestrator = self.make()
        orchestrator.start()
        first = orchestrator.state().elapsed_s
        time.sleep(0.05)
        self.assertGreater(orchestrator.state().elapsed_s, first)


class OrchestratorControlTests(OrchestratorTestCase):
    def test_second_start_is_ignored_while_running(self):
        orchestrator = self.make()
        self.assertTrue(orchestrator.start())
        self.assertFalse(orchestrator.start())

    def test_can_start_again_after_finishing(self):
        orchestrator = self.make()
        orchestrator.start()
        self.wait_phase(orchestrator, PHASE_PREVIEW)
        orchestrator.stop()
        self.camera = FakeCamera()
        self.assertTrue(orchestrator.start())
        self.assertIsNone(orchestrator.summary())

    def test_stop_during_calibration(self):
        orchestrator = self.make()
        orchestrator.start()
        self.wait_phase(orchestrator, PHASE_PREVIEW)
        orchestrator.send_command('calibrate')
        self.wait_phase(orchestrator, PHASE_CALIBRATING)
        orchestrator.stop()
        self.assertEqual(orchestrator.state().phase, PHASE_FINISHED)
        self.assertIsNone(orchestrator.state().error)

    def test_stop_without_start_is_safe(self):
        self.make().stop()

    def test_unknown_command_raises(self):
        with self.assertRaises(ValueError):
            self.make().send_command('borrar')


class OrchestratorErrorTests(OrchestratorTestCase):
    def test_camera_error(self):
        self.camera = FakeCamera(fail=True)
        orchestrator = self.make()
        orchestrator.start()
        self.wait_phase(orchestrator, PHASE_ERROR)
        self.assertIn('No se pudo abrir la cámara', orchestrator.state().error)
        self.assertTrue(wait_until(lambda: not orchestrator.is_running))
        self.assertFalse(self.bracelet.is_connected)

    def test_failed_calibration_returns_to_camera_with_notice(self):
        self.face = None
        orchestrator = self.make()
        orchestrator.start()
        self.wait_phase(orchestrator, PHASE_PREVIEW)
        orchestrator.send_command('calibrate')
        self.assertTrue(wait_until(lambda: orchestrator.state().notice is not None))
        self.assertEqual(orchestrator.state().phase, PHASE_PREVIEW)
        self.assertIn('Calibración incompleta', orchestrator.state().notice)

        self.face = OPEN_FACE  # Segundo intento, ahora con rostro.
        orchestrator.send_command('calibrate')
        self.wait_phase(orchestrator, PHASE_CALIBRATED)
        self.assertIsNone(orchestrator.state().notice)

    def test_unexpected_error_does_not_kill_silently(self):
        def broken():
            raise RuntimeError('fallo raro')

        self.face = broken
        orchestrator = self.make()
        with self.assertLogs('monitoring.orchestrator', level='ERROR'):
            orchestrator.start()
            self.wait_phase(orchestrator, PHASE_ERROR)
        self.assertIn('fallo raro', orchestrator.state().error)


class ViewTests(OrchestratorTestCase):
    def setUp(self):
        super().setUp()
        self.orchestrator = self.make()
        self.remote = FakeRemoteQueue(available=False)
        for target, value in (('monitoring.views.get_orchestrator', self.orchestrator),
                              ('monitoring.views.get_registry_queue', self.remote)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def finish_session(self):
        self.run_to_monitoring(self.orchestrator)
        self.face = CLOSED_FACE
        self.assertTrue(wait_until(lambda: self.orchestrator.state().level >= 1, timeout=10))
        self.client.post(reverse('monitoring:finish'))

    def test_welcome_and_prepare_pages(self):
        self.assertContains(self.client.get(reverse('monitoring:welcome')), 'Entrar al sistema')
        response = self.client.get(reverse('monitoring:prepare'))
        self.assertContains(response, 'Iniciar monitoreo')
        self.assertContains(response, 'Pulsera real')
        self.assertContains(response, 'value="simulated" checked')

    def test_start_rejects_unknown_bracelet_mode(self):
        response = self.client.post(reverse('monitoring:start'), {'bracelet_mode': 'otro'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.orchestrator.is_running)

    def test_start_requires_post(self):
        self.assertEqual(self.client.get(reverse('monitoring:start')).status_code, 405)

    def test_start_opens_the_session_page(self):
        response = self.client.post(reverse('monitoring:start'), {'bracelet_mode': 'simulated'})
        self.assertRedirects(response, reverse('monitoring:session'))
        self.assertTrue(self.orchestrator.is_running)
        page = self.client.get(reverse('monitoring:session'))
        for screen in ('camera', 'bracelet', 'calibrating', 'calibrated', 'monitor',
                       'degraded', 'disconnected', 'error'):
            self.assertContains(page, f'data-screen="{screen}"')

    def test_prepare_redirects_while_running(self):
        self.orchestrator.start()
        self.assertRedirects(self.client.get(reverse('monitoring:prepare')),
                             reverse('monitoring:session'))

    def test_session_without_start_goes_to_prepare(self):
        self.assertRedirects(self.client.get(reverse('monitoring:session')),
                             reverse('monitoring:prepare'))

    def test_state_json(self):
        self.orchestrator.start()
        self.wait_phase(self.orchestrator, PHASE_PREVIEW)
        data = self.client.get(reverse('monitoring:state')).json()
        self.assertEqual(data['phase'], PHASE_PREVIEW)
        self.assertTrue(data['running'])

    def test_commands(self):
        self.orchestrator.start()
        self.wait_phase(self.orchestrator, PHASE_PREVIEW)
        url = reverse('monitoring:command')
        self.assertEqual(self.client.post(url, {'action': 'calibrate'}).status_code, 200)
        self.wait_phase(self.orchestrator, PHASE_CALIBRATED)
        self.assertEqual(self.client.post(url, {'action': 'borrar'}).status_code, 400)

        self.assertTrue(wait_until(lambda: self.bracelet.is_connected))
        self.client.post(url, {'action': 'simulate_disconnect'})
        self.assertTrue(wait_until(lambda: not self.orchestrator.state().bracelet.connected))
        self.client.post(url, {'action': 'simulate_reconnect'})
        self.assertTrue(wait_until(lambda: self.orchestrator.state().bracelet.connected))

    async def test_video_stream_sends_jpeg_frames(self):
        """El video es un generador asíncrono: con Daphne no ocupa el hilo de las vistas."""
        from asgiref.sync import sync_to_async
        from django.test import AsyncClient

        await sync_to_async(self.orchestrator.start)()
        has_frame = await sync_to_async(wait_until)(
            lambda: self.orchestrator.latest_jpeg() is not None)
        self.assertTrue(has_frame)
        response = await AsyncClient().get(reverse('monitoring:video'))
        self.assertTrue(response['Content-Type'].startswith('multipart/x-mixed-replace'))
        chunk = await anext(aiter(response.streaming_content))
        self.assertTrue(chunk.startswith(b'--frame'))
        self.assertIn(b'\xff\xd8', chunk)
        await sync_to_async(self.orchestrator.stop)()

    def test_finish_shows_summary_and_csv(self):
        self.finish_session()
        self.assertFalse(self.orchestrator.is_running)
        response = self.client.get(reverse('monitoring:summary'))
        self.assertContains(response, 'Sesión finalizada')
        self.assertContains(response, 'Señal inicial')
        self.assertContains(response, 'no respondió')  # M4 apagado: datos en memoria.

        csv_response = self.client.get(reverse('monitoring:summary_csv'))
        self.assertEqual(csv_response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="vigia_sesion_', csv_response['Content-Disposition'])
        text = csv_response.content.decode('utf-8')
        self.assertTrue(text.startswith('﻿hora,nivel,estado,ear,mar'))
        self.assertIn(',1,Señal inicial', text)

    def test_summary_without_session_goes_to_prepare(self):
        self.assertRedirects(self.client.get(reverse('monitoring:summary')),
                             reverse('monitoring:prepare'))
        self.assertRedirects(self.client.get(reverse('monitoring:summary_csv')),
                             reverse('monitoring:prepare'))

    def test_summary_and_csv_come_from_m4_when_available(self):
        self.finish_session()
        session_id = self.orchestrator.summary().session_id
        self.remote.available = True
        self.remote.client = FakeRemoteClient(
            summary={'session_id': session_id, 'started_at': '2026-11-10T15:00:00+00:00',
                     'duration_s': 75.0, 'alert_count': 4, 'max_level': 2, 'threshold': 0.22},
            events=[{'at': '2026-11-10T15:00:10+00:00', 'level': 2, 'status': 'Atención reducida'}],
            csv='hora,nivel,estado,ear,mar\r\n2026-11-10 10:00:10,2,desde M4,0.100,0.050\r\n'.encode(),
        )
        response = self.client.get(reverse('monitoring:summary'))
        self.assertContains(response, 'Datos guardados en el servicio de registro (M4)')
        self.assertContains(response, '00:01:15')
        self.assertContains(response, 'Atención reducida')
        self.assertContains(response, '10:00:10')  # Hora local de Guayaquil.
        self.assertEqual(response.context['summary']['alert_count'], 4)

        text = self.client.get(reverse('monitoring:summary_csv')).content.decode('utf-8')
        self.assertIn('desde M4', text)


# ---------- M4: registros falsos ----------

class RecordingRegistry:
    """Registro falso que guarda lo que el orquestador encola."""

    def __init__(self):
        self.calls = []

    def start_session(self, session_id, started_at, bracelet_mode=None):
        self.calls.append(('session', session_id, bracelet_mode))

    def add_event(self, session_id, event):
        self.calls.append(('event', session_id, event))

    def finish_session(self, session_id, ended_at, threshold=None, stats=None):
        self.calls.append(('finish', session_id, threshold))
        self.stats = stats


class FakeRemoteClient:
    def __init__(self, summary=None, events=(), csv=b''):
        self._summary, self._events, self._csv = summary, list(events), csv

    def summary(self, session_id):
        return self._summary

    def events(self, session_id):
        return self._events

    def events_csv(self, session_id):
        return self._csv


class FakeRemoteQueue:
    """Lo que las vistas usan de la cola: flush(), status() y client."""

    def __init__(self, available, client=None):
        self.available = available
        self.client = client or FakeRemoteClient()

    def flush(self, timeout):
        return self.available

    def status(self):
        return RegistryStatus(self.available, 0, 0, 0, None)


class OrchestratorRegistryTests(OrchestratorTestCase):
    def test_session_events_and_finish_are_queued_in_order(self):
        orchestrator = self.make()
        self.run_to_monitoring(orchestrator)
        self.face = CLOSED_FACE
        self.assertTrue(wait_until(lambda: orchestrator.state().level == 3, timeout=10))
        orchestrator.stop()

        calls = self.registry.calls
        self.assertEqual(calls[0][0], 'session')
        self.assertEqual(calls[-1][0], 'finish')
        session_id = calls[0][1]
        self.assertEqual(orchestrator.summary().session_id, session_id)
        self.assertTrue(all(call[1] == session_id for call in calls))
        self.assertEqual(calls[0][2], 'simulated')
        self.assertAlmostEqual(calls[-1][2], 0.30)

        events = [call[2] for call in calls if call[0] == 'event']
        self.assertEqual([event['level'] for event in events], [1, 2, 3])
        self.assertAlmostEqual(events[0]['ear'], 0.04)
        self.assertEqual(events[0]['mar'], 0.0)
        self.assertEqual(len({event['id'] for event in events}), 3)
        self.assertNotIn('image', events[0])  # RNF-05: sólo métricas.


# ---------- Fuerza de vibración, «Finalizar» en dos pasos y RNF-02 ----------

class StrengthTests(OrchestratorTestCase):
    def test_strength_reaches_the_bracelet_during_the_session(self):
        orchestrator = self.make()
        self.run_to_monitoring(orchestrator)
        self.assertEqual(orchestrator.state().vibration_strength, 3)
        orchestrator.set_strength(5)
        self.assertTrue(wait_until(lambda: self.bracelet.strength == 5))
        self.assertEqual(orchestrator.state().vibration_strength, 5)
        self.assertEqual(orchestrator.state().bracelet.strength, 5)

    def test_strength_chosen_before_starting_is_used(self):
        orchestrator = self.make()
        orchestrator.set_strength(1)
        orchestrator.start()
        self.assertTrue(wait_until(lambda: self.bracelet.messages() != []))
        self.assertEqual(self.bracelet.messages()[0].strength, 1)

    def test_invalid_strength_raises(self):
        with self.assertRaises(ValueError):
            self.make().set_strength(7)


class Rnf02StatsTests(OrchestratorTestCase):
    def test_monitoring_time_and_fps_are_measured(self):
        orchestrator = self.make()
        self.run_to_monitoring(orchestrator)
        time.sleep(0.2)
        orchestrator.stop()
        summary = orchestrator.summary()
        self.assertGreater(summary.monitoring_s, 0)
        self.assertLess(summary.monitoring_s, summary.duration_s)  # Sin preparar ni calibrar.
        self.assertIsNotNone(summary.fps_avg)
        self.assertFalse(summary.rnf02_passed)  # Mucho menos de 5 minutos.
        self.assertEqual(set(self.registry.stats), {'monitoring_s', 'fps_avg', 'fps_min'})

    def test_rnf02_rule(self):
        from monitoring.orchestrator import SessionSummary
        from django.utils import timezone
        now = timezone.now()

        def summary(monitoring_s, fps_min):
            return SessionSummary(now, now, monitoring_s + 20, 0, 0,
                                  monitoring_s=monitoring_s, fps_min=fps_min)

        self.assertTrue(summary(300, 25.0).rnf02_passed)
        self.assertFalse(summary(299, 30.0).rnf02_passed)
        self.assertFalse(summary(600, 24.9).rnf02_passed)
        self.assertFalse(summary(600, None).rnf02_passed)

    def test_rnf02_text(self):
        from monitoring.presenters import rnf02_text
        self.assertIn('cumplida', rnf02_text(310, 27))
        self.assertIn('faltan 3.0 min', rnf02_text(120, 29))
        self.assertIn('no cumplida', rnf02_text(310, 20))
        self.assertIn('sin datos', rnf02_text(0, None))


class TwoStepFinishTests(ViewTests):
    """Primer «Finalizar sesión»: resumen en vivo con el monitoreo activo.
    Botón «Finalizar»: lo detiene todo."""

    def test_summary_while_monitoring_is_live_and_keeps_running(self):
        self.run_to_monitoring(self.orchestrator)
        self.face = CLOSED_FACE
        self.assertTrue(wait_until(lambda: self.orchestrator.state().level >= 1, timeout=10))

        response = self.client.get(reverse('monitoring:summary'))
        self.assertTrue(response.context['live'])
        self.assertContains(response, 'Monitoreo en curso')
        self.assertContains(response, 'Volver al monitoreo')
        self.assertContains(response, reverse('monitoring:finish'))
        self.assertNotContains(response, 'Exportar CSV')
        self.assertGreaterEqual(response.context['summary']['alert_count'], 1)
        time.sleep(0.1)
        self.assertTrue(self.orchestrator.is_running)  # La detección sigue.
        self.assertEqual(self.orchestrator.state().phase, PHASE_MONITORING)

        # El CSV se exporta al finalizar del todo.
        self.assertRedirects(self.client.get(reverse('monitoring:summary_csv')),
                             reverse('monitoring:summary'), fetch_redirect_response=False)

        self.client.post(reverse('monitoring:finish'))
        self.assertFalse(self.orchestrator.is_running)
        final = self.client.get(reverse('monitoring:summary'))
        self.assertFalse(final.context['live'])
        self.assertContains(final, 'Sesión finalizada')
        self.assertContains(final, 'Exportar CSV')

    def test_summary_before_monitoring_goes_back_to_the_session(self):
        self.orchestrator.start()
        self.wait_phase(self.orchestrator, PHASE_PREVIEW)
        self.assertRedirects(self.client.get(reverse('monitoring:summary')),
                             reverse('monitoring:session'), fetch_redirect_response=False)

    def test_state_json_carries_the_live_summary(self):
        self.run_to_monitoring(self.orchestrator)
        data = self.client.get(reverse('monitoring:state')).json()
        self.assertEqual(data['summary']['alert_count'], 0)
        self.assertIn('rnf02', data['summary'])
        self.assertEqual(data['vibration_strength'], 3)

    def test_strength_command(self):
        url = reverse('monitoring:command')
        self.assertEqual(self.client.post(url, {'action': 'strength', 'value': '4'}).status_code, 200)
        self.assertEqual(self.orchestrator.state().vibration_strength, 4)
        for bad in ('0', '6', 'mucho', ''):
            self.assertEqual(
                self.client.post(url, {'action': 'strength', 'value': bad}).status_code, 400)

    def test_session_page_has_slider_and_live_finish_links(self):
        self.orchestrator.start()
        page = self.client.get(reverse('monitoring:session')).content.decode()
        self.assertEqual(page.count('data-strength-input'), 2)  # Pulsera y monitoreo.
        self.assertIn('data-socket-path="/ws/sesion/"', page)
        self.assertIn(f'href="{reverse("monitoring:summary")}">Finalizar sesión', page)


class SessionConsumerTests(OrchestratorTestCase):
    """El WebSocket empuja el estado sin que la página lo pida."""

    async def test_pushes_state_and_live_summary(self):
        from asgiref.sync import sync_to_async
        from channels.testing import WebsocketCommunicator

        from monitoring.consumers import SessionConsumer

        orchestrator = self.make()
        await sync_to_async(self.run_to_monitoring)(orchestrator)
        remote = FakeRemoteQueue(available=True)
        with mock.patch('monitoring.consumers.get_orchestrator', return_value=orchestrator), \
                mock.patch('monitoring.consumers.get_registry_queue', return_value=remote):
            communicator = WebsocketCommunicator(SessionConsumer.as_asgi(), '/ws/sesion/')
            connected, _ = await communicator.connect()
            self.assertTrue(connected)
            first = await communicator.receive_json_from(timeout=2)
            self.assertEqual(first['phase'], PHASE_MONITORING)
            self.assertTrue(first['running'])
            self.assertIsNotNone(first['summary'])
            second = await communicator.receive_json_from(timeout=2)  # Llega sola.
            self.assertIn('ear', second)
            await communicator.disconnect()
        await sync_to_async(orchestrator.stop)()


# ---------- Varios rostros: cuál se mide ----------

def placed(points, scale, dx, dy):
    """Un rostro sintético agrandado y movido (el EAR no cambia al escalar).
    Se le agregan frente y mentón (puntos 10 y 152) para que su recuadro tenga
    el tamaño de una cara y no sólo el de los ojos."""
    points = points.copy()
    points[10], points[152] = (5, -8), (5, 12)
    return points * scale + np.array([dx, dy])


class MultiFaceDetector:
    """Devuelve varios rostros, como FaceLandmarkDetector.detect_all."""

    def __init__(self, faces_for):
        self.faces_for = faces_for

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def detect_all(self, image, timestamp_ms):
        return self.faces_for()


class MultiFaceTests(OrchestratorTestCase):
    def make_multi(self):
        orchestrator = Orchestrator(
            camera_factory=lambda: self.camera,
            detector_factory=lambda: MultiFaceDetector(lambda: self.faces),
            bracelet_factory=lambda mode: self.bracelet,
            registry=self.registry,
        )
        self.addCleanup(orchestrator.stop)
        return orchestrator

    def test_measures_the_largest_face_and_counts_them(self):
        small_open = placed(OPEN_FACE, 1, 0, 0)  # EAR 0.4
        big_closed = placed(CLOSED_FACE, 3, 30, 0)  # EAR 0.04, tres veces más grande
        self.faces = [small_open, big_closed]
        orchestrator = self.make_multi()
        orchestrator.start()
        self.wait_phase(orchestrator, PHASE_PREVIEW)
        self.assertTrue(wait_until(lambda: orchestrator.state().face_count == 2))
        self.assertAlmostEqual(orchestrator.state().ear, 0.04)

    def test_does_not_jump_to_a_similar_face(self):
        orchestrator = self.make_multi()
        a = placed(OPEN_FACE, 2, 0, 0)  # EAR 0.4
        b = placed(CLOSED_FACE, 2.2, 40, 0)  # EAR 0.04, apenas 21 % más grande
        self.faces = [a]
        self.assertEqual(orchestrator._pick_main_face([a]), 0)
        self.assertEqual(orchestrator._pick_main_face([a, b]), 0)  # Sigue con «a».
        c = placed(CLOSED_FACE, 4, 40, 0)  # Cuatro veces el área de «a»: se cambia.
        self.assertEqual(orchestrator._pick_main_face([a, c]), 1)

    def test_without_faces_there_is_no_main_face(self):
        orchestrator = self.make_multi()
        self.assertIsNone(orchestrator._pick_main_face([]))
        self.assertIsNone(orchestrator._main_center)

    def test_preview_marks_the_measured_face_in_green(self):
        import cv2

        orchestrator = self.make_multi()
        image = np.full((120, 160, 3), 40, dtype=np.uint8)
        face = placed(OPEN_FACE, 4, 20, 40)  # Recuadro de x 20..60, y 32..48
        other = placed(OPEN_FACE, 2, 110, 40)
        orchestrator._publish_preview(image, [face, other], 0)
        decoded = cv2.imdecode(np.frombuffer(orchestrator.latest_jpeg(), np.uint8), cv2.IMREAD_COLOR)
        # En espejo, el borde izquierdo del rostro medido queda en x = 160 - 60 = 100.
        b, g, r = decoded[40, 100].astype(int)
        self.assertGreater(g, r + 30)
        self.assertGreater(g, b + 30)
        self.assertTrue((image == 40).all())  # La imagen original no se modifica.
