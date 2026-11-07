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

    def make(self):
        orchestrator = Orchestrator(
            camera_factory=lambda: self.camera,
            detector_factory=lambda: FakeDetector(self.current_face),
            bracelet_factory=lambda mode: self.bracelet,
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
        patcher = mock.patch('monitoring.views.get_orchestrator', return_value=self.orchestrator)
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

    def test_video_stream_sends_jpeg_frames(self):
        self.orchestrator.start()
        self.assertTrue(wait_until(lambda: self.orchestrator.latest_jpeg() is not None))
        response = self.client.get(reverse('monitoring:video'))
        self.assertTrue(response['Content-Type'].startswith('multipart/x-mixed-replace'))
        chunk = next(iter(response.streaming_content))
        self.assertTrue(chunk.startswith(b'--frame'))
        self.assertIn(b'\xff\xd8', chunk)
        self.orchestrator.stop()
        response.close()

    def test_finish_shows_summary_and_csv(self):
        self.finish_session()
        self.assertFalse(self.orchestrator.is_running)
        response = self.client.get(reverse('monitoring:summary'))
        self.assertContains(response, 'Sesión finalizada')
        self.assertContains(response, 'Señal inicial')

        csv_response = self.client.get(reverse('monitoring:summary_csv'))
        self.assertEqual(csv_response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="vigia_sesion_', csv_response['Content-Disposition'])
        text = csv_response.content.decode('utf-8')
        self.assertTrue(text.startswith('﻿hora,nivel,estado'))
        self.assertIn(',1,Señal inicial', text)

    def test_summary_without_session_goes_to_prepare(self):
        self.assertRedirects(self.client.get(reverse('monitoring:summary')),
                             reverse('monitoring:prepare'))
        self.assertRedirects(self.client.get(reverse('monitoring:summary_csv')),
                             reverse('monitoring:prepare'))
