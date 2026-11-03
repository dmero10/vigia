"""Pruebas de M2. Usan datos sintéticos: no necesitan cámara ni un rostro real."""

from io import StringIO
from types import SimpleNamespace
from unittest import mock

import numpy as np
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from analysis import landmarks as landmarks_module
from analysis.calibration import CalibrationError, EarCalibrator
from analysis.classifier import (
    DEGRADED_LOW_LIGHT,
    DEGRADED_NO_FACE,
    DrowsinessClassifier,
)
from analysis.landmarks import LandmarkError, ensure_model, to_pixels
from analysis.metrics import LEFT_EYE, RIGHT_EYE, compute_ear, compute_mar

THRESHOLD = 0.2
OPEN = 0.3
CLOSED = 0.1
MOUTH_CLOSED = 0.2
MOUTH_OPEN = 0.8


def set_eye(points, eye, half_height):
    p1, p2, p3, p4, p5, p6 = eye
    points[p1], points[p4] = (0, 0), (10, 0)
    points[p2], points[p6] = (3, half_height), (3, -half_height)
    points[p3], points[p5] = (7, half_height), (7, -half_height)


class MetricsTests(SimpleTestCase):
    def setUp(self):
        self.points = np.zeros((478, 2))

    def test_ear_averages_both_eyes(self):
        set_eye(self.points, RIGHT_EYE, 2)  # EAR = (4 + 4) / 20 = 0.4
        set_eye(self.points, LEFT_EYE, 1)  # EAR = (2 + 2) / 20 = 0.2
        self.assertAlmostEqual(compute_ear(self.points), 0.3)

    def test_mar(self):
        self.points[61], self.points[291] = (0, 0), (10, 0)
        for (top, bottom), height in zip(((81, 178), (13, 14), (311, 402)), (6, 8, 10)):
            self.points[top], self.points[bottom] = (5, 0), (5, height)
        self.assertAlmostEqual(compute_mar(self.points), 0.8)

    def test_zero_width_does_not_divide_by_zero(self):
        self.assertEqual(compute_ear(self.points), 0.0)
        self.assertEqual(compute_mar(self.points), 0.0)


class LandmarksTests(SimpleTestCase):
    def test_to_pixels(self):
        points = [SimpleNamespace(x=0.5, y=0.25), SimpleNamespace(x=1.0, y=0.0)]
        np.testing.assert_allclose(to_pixels(points, 640, 480), [[320, 120], [640, 0]])

    def test_existing_model_is_not_downloaded(self):
        with mock.patch.object(landmarks_module.urllib.request, 'urlretrieve') as download:
            with mock.patch.object(landmarks_module.Path, 'exists', return_value=True):
                ensure_model('modelo.task')
        download.assert_not_called()

    def test_failed_download_raises_clear_error(self):
        error = landmarks_module.urllib.error.URLError('sin red')
        with mock.patch.object(landmarks_module.urllib.request, 'urlretrieve', side_effect=error):
            with mock.patch.object(landmarks_module.Path, 'exists', return_value=False):
                with mock.patch.object(landmarks_module.Path, 'mkdir'):
                    with self.assertRaisesMessage(LandmarkError, 'No se pudo descargar'):
                        ensure_model('modelo.task')


class CalibrationTests(SimpleTestCase):
    def calibrate(self, ear, frames=60):
        calibrator = EarCalibrator()
        for i in range(frames):
            calibrator.update(i * 5000 // (frames - 1), ear)
        return calibrator

    def test_threshold_is_median_times_factor(self):
        result = self.calibrate(0.28).result()
        self.assertAlmostEqual(result.threshold, 0.21)
        self.assertAlmostEqual(result.median_ear, 0.28)

    def test_median_ignores_a_blink(self):
        calibrator = EarCalibrator()
        for i in range(40):
            calibrator.update(i * 100, 0.05 if i < 3 else 0.28)
        self.assertAlmostEqual(calibrator.result().threshold, 0.21)

    def test_threshold_limits(self):
        self.assertEqual(self.calibrate(0.10).result().threshold, 0.12)
        self.assertEqual(self.calibrate(0.60).result().threshold, 0.30)

    def test_finishes_at_five_seconds(self):
        calibrator = EarCalibrator()
        self.assertFalse(calibrator.is_finished)
        calibrator.update(1000, 0.3)
        calibrator.update(5999, 0.3)
        self.assertFalse(calibrator.is_finished)
        calibrator.update(6000, 0.3)
        self.assertTrue(calibrator.is_finished)

    def test_too_few_samples_raises(self):
        # 30 fotogramas, pero el último cae justo en 5 s y ya queda fuera.
        with self.assertRaisesMessage(CalibrationError, '29 muestras'):
            self.calibrate(0.28, frames=30).result()

    def test_exactly_min_samples_works(self):
        calibrator = EarCalibrator()
        for i in range(30):
            calibrator.update(i, 0.28)
        calibrator.update(5000)
        self.assertEqual(calibrator.result().samples, 30)

    def test_finishes_without_face_and_reports_it(self):
        calibrator = EarCalibrator()
        calibrator.update(0)
        calibrator.update(5000)
        self.assertTrue(calibrator.is_finished)
        with self.assertRaises(CalibrationError):
            calibrator.result()

    def test_samples_after_the_end_are_ignored(self):
        calibrator = self.calibrate(0.28)
        samples = calibrator.result().samples
        calibrator.update(6000, 0.9)
        self.assertEqual(calibrator.result().samples, samples)


class ClassifierTestCase(SimpleTestCase):
    def setUp(self):
        self.classifier = DrowsinessClassifier(THRESHOLD)

    def feed(self, start_ms, end_ms, ear=OPEN, mar=MOUTH_CLOSED, light_ok=True, step=50):
        """Envía fotogramas de start a end (incluido) y devuelve el último resultado."""
        assessment = None
        for timestamp_ms in range(start_ms, end_ms + 1, step):
            assessment = self.classifier.update(timestamp_ms, ear, mar, light_ok)
        return assessment


class ClosureTests(ClassifierTestCase):
    def test_open_eyes_are_level_zero(self):
        self.assertEqual(self.feed(0, 10000).level, 0)

    def test_level_boundaries(self):
        self.feed(0, 0, ear=CLOSED)
        cases = ((1499, 0), (1500, 1), (2999, 1), (3000, 2), (4999, 2), (5000, 3), (9000, 3))
        for timestamp_ms, expected in cases:
            with self.subTest(timestamp_ms=timestamp_ms):
                assessment = self.classifier.update(timestamp_ms, CLOSED, MOUTH_CLOSED)
                self.assertEqual(assessment.level, expected)
                self.assertEqual(assessment.closed_ms, timestamp_ms)

    def test_ear_equal_to_threshold_is_open(self):
        self.assertEqual(self.feed(0, 2000, ear=THRESHOLD).level, 0)

    def test_normal_blink_does_not_alert(self):
        self.assertEqual(self.feed(0, 300, ear=CLOSED).level, 0)
        self.assertEqual(self.feed(350, 1000).closed_ms, 0)

    def test_reopening_returns_to_zero_after_200_ms(self):
        self.assertEqual(self.feed(0, 3000, ear=CLOSED).level, 2)
        self.assertEqual(self.classifier.update(3050, OPEN, MOUTH_CLOSED).level, 2)
        self.assertEqual(self.classifier.update(3249, OPEN, MOUTH_CLOSED).level, 2)
        self.assertEqual(self.classifier.update(3250, OPEN, MOUTH_CLOSED).level, 0)

    def test_short_noise_does_not_end_the_closure(self):
        self.feed(0, 1000, ear=CLOSED)
        self.feed(1050, 1200)  # Sólo 150 ms con EAR alto.
        assessment = self.feed(1250, 1500, ear=CLOSED)
        self.assertEqual(assessment.closed_ms, 1500)
        self.assertEqual(assessment.level, 1)


class YawnTests(ClassifierTestCase):
    def yawn(self, start_ms, duration_ms=1000):
        return self.feed(start_ms, start_ms + duration_ms, mar=MOUTH_OPEN)

    def close_mouth(self, timestamp_ms):
        return self.classifier.update(timestamp_ms, OPEN, MOUTH_CLOSED)

    def test_counts_after_one_second(self):
        self.assertEqual(self.yawn(0, duration_ms=950).yawn_count, 0)
        self.assertEqual(self.classifier.update(1000, OPEN, MOUTH_OPEN).yawn_count, 1)

    def test_mar_equal_to_limit_is_not_a_yawn(self):
        self.assertEqual(self.feed(0, 2000, mar=0.6).yawn_count, 0)

    def test_long_yawn_counts_once(self):
        self.assertEqual(self.yawn(0, duration_ms=10000).yawn_count, 1)

    def test_two_yawns_do_not_alert(self):
        self.yawn(0)
        self.close_mouth(1100)
        assessment = self.yawn(2000)
        self.assertEqual(assessment.yawn_count, 2)
        self.assertEqual(assessment.level, 0)

    def test_third_yawn_alerts_for_three_seconds_and_resets(self):
        for start_ms in (0, 2000):
            self.yawn(start_ms)
            self.close_mouth(start_ms + 1100)
        assessment = self.yawn(4000)  # Se cuenta en t = 5000.
        self.assertEqual(assessment.level, 1)
        self.assertEqual(assessment.yawn_count, 0)
        self.assertEqual(self.close_mouth(7999).level, 1)
        self.assertEqual(self.close_mouth(8000).level, 0)

    def test_old_yawns_leave_the_window(self):
        self.yawn(0)  # Se cuenta en t = 1000.
        self.close_mouth(1100)
        self.assertEqual(self.close_mouth(301000).yawn_count, 1)
        self.assertEqual(self.close_mouth(301001).yawn_count, 0)

    def test_yawns_spread_over_more_than_five_minutes_do_not_alert(self):
        for start_ms in (0, 200000, 400000):
            assessment = self.yawn(start_ms)
            self.close_mouth(start_ms + 1100)
        self.assertEqual(assessment.level, 0)
        self.assertEqual(assessment.yawn_count, 2)

    def test_closure_level_wins_over_yawn_alert(self):
        for start_ms in (0, 2000):
            self.yawn(start_ms)
            self.close_mouth(start_ms + 1100)
        self.feed(4000, 4950, ear=CLOSED, mar=MOUTH_OPEN)
        self.feed(5000, 7000, ear=CLOSED, mar=MOUTH_OPEN)
        self.assertEqual(self.classifier.update(7050, CLOSED, MOUTH_CLOSED).level, 2)


class OpenMouthTests(ClassifierTestCase):
    """Usa los valores medidos con la webcam: umbral 0.22 y ojos abiertos al bostezar."""

    def setUp(self):
        self.classifier = DrowsinessClassifier(0.22)

    def test_yawn_with_open_eyes_does_not_alert(self):
        self.assertEqual(self.feed(0, 10000, ear=0.163, mar=0.585).level, 0)

    def test_after_yawn_with_open_eyes_does_not_alert(self):
        self.feed(0, 5000, ear=0.163, mar=0.585)
        self.assertEqual(self.feed(5050, 7900, ear=0.185, mar=0.008).level, 0)

    def test_closed_eyes_during_yawn_still_alert(self):
        assessment = self.feed(0, 3000, ear=0.044, mar=0.6)
        self.assertEqual(assessment.level, 2)

    def test_normal_threshold_returns_after_recovery(self):
        self.feed(0, 1000, ear=0.3, mar=0.585)  # Se recupera en t = 4000.
        self.assertEqual(self.classifier.update(3999, 0.185, 0.01).closed_ms, 0)
        self.classifier.update(4000, 0.185, 0.01)
        self.assertEqual(self.classifier.update(5500, 0.185, 0.01).level, 1)

    def test_mar_at_limit_keeps_normal_threshold(self):
        self.assertEqual(self.feed(0, 1500, ear=0.185, mar=0.5).level, 1)

    def test_opening_mouth_ends_a_false_closure(self):
        self.feed(0, 1000, ear=0.185, mar=0.01)
        self.assertEqual(self.feed(1050, 1250, ear=0.185, mar=0.585).closed_ms, 0)

    def test_degraded_clears_recovery(self):
        self.feed(0, 1000, ear=0.3, mar=0.585)
        self.classifier.update(1050, 0.3, 0.01, light_ok=False)
        self.classifier.update(1100, 0.185, 0.01)
        self.assertEqual(self.classifier.update(2600, 0.185, 0.01).level, 1)


class DegradedTests(ClassifierTestCase):
    def test_low_light_is_immediate_and_clears_the_alert(self):
        self.assertEqual(self.feed(0, 3000, ear=CLOSED).level, 2)
        assessment = self.classifier.update(3050, CLOSED, MOUTH_CLOSED, light_ok=False)
        self.assertEqual(assessment.level, 0)
        self.assertEqual(assessment.degraded_reason, DEGRADED_LOW_LIGHT)
        self.assertTrue(assessment.is_degraded)

    def test_closure_restarts_after_low_light(self):
        self.feed(0, 3000, ear=CLOSED)
        self.classifier.update(3050, CLOSED, MOUTH_CLOSED, light_ok=False)
        assessment = self.classifier.update(3100, CLOSED, MOUTH_CLOSED)
        self.assertEqual(assessment.closed_ms, 0)
        self.assertEqual(assessment.level, 0)
        self.assertFalse(assessment.is_degraded)

    def test_no_face_keeps_state_up_to_half_a_second(self):
        self.assertEqual(self.feed(0, 3000, ear=CLOSED).level, 2)
        first = self.classifier.update(3050)
        at_limit = self.classifier.update(3550)
        self.assertEqual(first.level, 2)
        self.assertEqual(at_limit.level, 2)
        self.assertFalse(at_limit.is_degraded)

    def test_no_face_over_half_a_second_degrades(self):
        self.feed(0, 3000, ear=CLOSED)
        self.classifier.update(3050)
        assessment = self.classifier.update(3551)
        self.assertEqual(assessment.level, 0)
        self.assertEqual(assessment.degraded_reason, DEGRADED_NO_FACE)

    def test_no_face_at_timestamp_zero_is_tracked(self):
        self.classifier.update(0)
        self.assertEqual(self.classifier.update(501).degraded_reason, DEGRADED_NO_FACE)

    def test_face_returning_clears_degraded(self):
        self.classifier.update(0)
        self.classifier.update(501)
        self.assertFalse(self.classifier.update(550, OPEN, MOUTH_CLOSED).is_degraded)
        self.assertFalse(self.classifier.update(1000).is_degraded)

    def test_low_light_keeps_yawn_count(self):
        self.feed(0, 1000, mar=MOUTH_OPEN)
        self.classifier.update(1100, OPEN, MOUTH_CLOSED)
        self.assertEqual(
            self.classifier.update(1150, OPEN, MOUTH_CLOSED, light_ok=False).yawn_count, 1
        )

    def test_degraded_cancels_active_yawn_alert(self):
        for start_ms in (0, 2000, 4000):
            self.feed(start_ms, start_ms + 1000, mar=MOUTH_OPEN)
            self.classifier.update(start_ms + 1100, OPEN, MOUTH_CLOSED)
        self.classifier.update(5200, OPEN, MOUTH_CLOSED, light_ok=False)
        self.assertEqual(self.classifier.update(5300, OPEN, MOUTH_CLOSED).level, 0)


class FakeCamera:
    """Entrega imágenes grises con 33 ms entre fotogramas."""

    def __init__(self, index=0):
        self.index = index
        self.timestamp_ms = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def read(self):
        self.timestamp_ms += 33
        return SimpleNamespace(
            image=np.full((480, 640, 3), 100, dtype=np.uint8),
            timestamp_ms=self.timestamp_ms,
        )


class FakeDetector:
    """Devuelve siempre un rostro con EAR = 0.4 (o ninguno, según `points`)."""

    points = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def detect(self, image, timestamp_ms):
        return self.points


class CheckAnalysisCommandTests(SimpleTestCase):
    COMMAND = 'analysis.management.commands.check_analysis'

    def run_command(self, points):
        detector = type('Detector', (FakeDetector,), {'points': points})
        output = StringIO()
        with mock.patch(f'{self.COMMAND}.Camera', FakeCamera):
            with mock.patch(f'{self.COMMAND}.FaceLandmarkDetector', detector):
                call_command('check_analysis', seconds=0.05, stdout=output)
        return output.getvalue()

    def test_calibrates_and_monitors(self):
        points = np.zeros((478, 2))
        set_eye(points, RIGHT_EYE, 2)
        set_eye(points, LEFT_EYE, 2)
        output = self.run_command(points)
        self.assertIn('Umbral: 0.300', output)  # 0.4 x 0.75 = 0.30
        self.assertIn('Nivel máximo: 0', output)
        self.assertIn('FPS con análisis', output)

    def test_no_face_during_calibration_reports_error(self):
        with self.assertRaisesMessage(CommandError, 'Calibración incompleta'):
            self.run_command(None)
