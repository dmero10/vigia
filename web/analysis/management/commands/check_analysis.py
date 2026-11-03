"""Prueba M1 + M2 con la cámara real: calibra y muestra el nivel. No guarda imágenes.

Uso: python manage.py check_analysis --seconds 30
"""

import time

from django.core.management.base import BaseCommand, CommandError

from acquisition.camera import DEFAULT_INDEX, Camera, CameraError
from acquisition.validation import (
    MIN_FPS,
    has_enough_light,
    mean_brightness,
    meets_min_fps,
)
from analysis.calibration import CalibrationError, EarCalibrator
from analysis.classifier import DrowsinessClassifier
from analysis.landmarks import FaceLandmarkDetector, LandmarkError
from analysis.metrics import compute_ear, compute_mar


class Command(BaseCommand):
    help = 'Calibra el umbral y muestra EAR, MAR, nivel y FPS con la cámara real.'

    def add_arguments(self, parser):
        parser.add_argument('--index', type=int, default=DEFAULT_INDEX,
                            help='Número de la cámara (por defecto, %(default)s).')
        parser.add_argument('--seconds', type=float, default=30.0,
                            help='Segundos de monitoreo (por defecto, %(default)s).')

    def handle(self, *args, **options):
        if options['seconds'] <= 0:
            raise CommandError('--seconds debe ser mayor que 0.')

        try:
            self.stdout.write('Preparando el modelo de MediaPipe...')
            with FaceLandmarkDetector() as detector, Camera(index=options['index']) as camera:
                threshold = self._calibrate(camera, detector)
                self._monitor(camera, detector, threshold, options['seconds'])
        except (CameraError, CalibrationError, LandmarkError) as error:
            raise CommandError(str(error)) from error

    def _measure(self, camera, detector):
        """Lee y analiza un fotograma. Devuelve (ms, ear, mar, luz suficiente)."""
        frame = camera.read()
        light_ok = has_enough_light(mean_brightness(frame.image))
        landmarks = detector.detect(frame.image, frame.timestamp_ms)
        if landmarks is None:
            return frame.timestamp_ms, None, None, light_ok
        return frame.timestamp_ms, compute_ear(landmarks), compute_mar(landmarks), light_ok

    def _calibrate(self, camera, detector):
        self.stdout.write('Calibrando 5 s: mire a la cámara con los ojos abiertos...')
        calibrator = EarCalibrator()
        while not calibrator.is_finished:
            timestamp_ms, ear, _, light_ok = self._measure(camera, detector)
            calibrator.update(timestamp_ms, ear if light_ok else None)

        result = calibrator.result()
        self.stdout.write(
            f'Umbral: {result.threshold:.3f} '
            f'(mediana {result.median_ear:.3f}, {result.samples} muestras)'
        )
        return result.threshold

    def _monitor(self, camera, detector, threshold, seconds):
        self.stdout.write('Monitoreando. Pruebe cerrar los ojos o bostezar.')
        classifier = DrowsinessClassifier(threshold)
        start = last_print = time.monotonic()
        frames = 0
        highest_level = 0

        while (now := time.monotonic()) - start < seconds:
            timestamp_ms, ear, mar, light_ok = self._measure(camera, detector)
            assessment = classifier.update(timestamp_ms, ear, mar, light_ok)
            frames += 1
            highest_level = max(highest_level, assessment.level)

            if now - last_print >= 1:
                last_print = now
                status = assessment.degraded_reason or 'ok'
                ear_text = f'{ear:.3f}' if ear is not None else '-'
                mar_text = f'{mar:.3f}' if mar is not None else '-'
                self.stdout.write(
                    f'Nivel {assessment.level} | EAR {ear_text} | MAR {mar_text} | '
                    f'bostezos {assessment.yawn_count} | estado {status}'
                )

        fps = frames / (time.monotonic() - start)
        self.stdout.write(f'Nivel máximo: {highest_level}')
        text = f'FPS con análisis: {fps:.1f} (mínimo {MIN_FPS})'
        if meets_min_fps(fps):
            self.stdout.write(self.style.SUCCESS(f'[OK] {text}'))
        else:
            self.stdout.write(self.style.ERROR(f'[NO CUMPLE] {text}'))
