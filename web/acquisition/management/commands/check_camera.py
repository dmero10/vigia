"""Comprueba la cámara y mide sus FPS reales. No guarda imágenes (RNF-05).

Uso: python manage.py check_camera --index 0 --seconds 10
"""

import time

from django.core.management.base import BaseCommand, CommandError

from acquisition.camera import DEFAULT_INDEX, Camera, CameraError
from acquisition.validation import (
    MIN_BRIGHTNESS,
    MIN_FPS,
    has_enough_light,
    mean_brightness,
    meets_min_fps,
)

# Al encender, la cámara ajusta su exposición: esos primeros fotogramas no se miden.
WARMUP_SECONDS = 1.0


class Command(BaseCommand):
    help = 'Comprueba la cámara: mide FPS reales, resolución y luz media.'

    def add_arguments(self, parser):
        parser.add_argument('--index', type=int, default=DEFAULT_INDEX,
                            help='Número de la cámara (por defecto, %(default)s).')
        parser.add_argument('--seconds', type=float, default=5.0,
                            help='Segundos de medición (por defecto, %(default)s).')

    def handle(self, *args, **options):
        if options['seconds'] <= 0:
            raise CommandError('--seconds debe ser mayor que 0.')

        try:
            with Camera(index=options['index']) as camera:
                self.stdout.write(f'Cámara {camera.index} abierta. Midiendo...')
                self._discard_warmup(camera)
                self._measure(camera, options['seconds'])
        except CameraError as error:
            raise CommandError(str(error)) from error

    def _discard_warmup(self, camera):
        end = time.monotonic() + WARMUP_SECONDS
        while time.monotonic() < end:
            camera.read()

    def _measure(self, camera, seconds):
        image = camera.read().image
        start = time.monotonic()
        count = 1
        brightness_total = mean_brightness(image)

        while time.monotonic() - start < seconds:
            image = camera.read().image
            count += 1
            brightness_total += mean_brightness(image)

        fps = (count - 1) / (time.monotonic() - start)
        brightness = brightness_total / count
        height, width = image.shape[:2]

        self.stdout.write(f'Fotogramas medidos: {count}')
        self.stdout.write(f'Resolución: {width}x{height}')
        self._report(f'FPS reales: {fps:.1f} (mínimo {MIN_FPS})', meets_min_fps(fps))
        self._report(
            f'Luz media: {brightness:.1f} (mínimo {MIN_BRIGHTNESS})',
            has_enough_light(brightness),
        )

    def _report(self, text, passed):
        if passed:
            self.stdout.write(self.style.SUCCESS(f'[OK] {text}'))
        else:
            self.stdout.write(self.style.ERROR(f'[NO CUMPLE] {text}'))
