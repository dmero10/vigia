"""M1 - Infraestructura: acceso a la cámara con OpenCV.

Este módulo sólo entrega fotogramas en memoria. RNF-05: nunca los guarda en disco.

Las llamadas son bloqueantes, por lo que deben hacerse desde el hilo en segundo
plano del orquestador y nunca dentro de una vista HTTP.
"""

import sys
import time
from dataclasses import dataclass

import cv2
import numpy as np

DEFAULT_INDEX = 0
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_FPS = 30

# Intentos para recibir la primera imagen: algunas cámaras tardan en entregarla.
OPEN_READ_ATTEMPTS = 30

# En Windows se usa Media Foundation: medido en la webcam del proyecto, da unos
# 30 FPS, mientras que DirectShow se queda en 10-20 FPS (no cumpliría RF-01).
DEFAULT_BACKEND = cv2.CAP_MSMF if sys.platform == 'win32' else cv2.CAP_ANY


class CameraError(Exception):
    """La cámara no se pudo abrir o dejó de entregar imágenes."""


@dataclass(frozen=True)
class Frame:
    """Fotograma capturado, sólo en memoria."""

    image: np.ndarray  # Imagen BGR, tal como la entrega OpenCV.
    timestamp_ms: int  # Estrictamente creciente, como exige detect_for_video.


class Camera:
    """Cámara web. Se puede usar con `with Camera() as camera:`."""

    def __init__(
        self,
        index=DEFAULT_INDEX,
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        fps=DEFAULT_FPS,
        backend=DEFAULT_BACKEND,
        capture_factory=cv2.VideoCapture,
    ):
        self.index = index
        self.width = width
        self.height = height
        self.fps = fps
        self.backend = backend
        # Se puede reemplazar en las pruebas para no necesitar una cámara real.
        self._capture_factory = capture_factory
        self._capture = None
        self._start_time = 0.0
        self._last_timestamp_ms = -1

    @property
    def is_open(self):
        return self._capture is not None

    def open(self):
        """Abre la cámara y comprueba que entregue imágenes."""
        if self.is_open:
            return

        capture = self._capture_factory(self.index, self.backend)
        if not capture.isOpened():
            capture.release()
            raise CameraError(
                f'No se pudo abrir la cámara {self.index}. '
                'Revise que esté conectada y que Windows permita usarla.'
            )

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.fps)

        for _ in range(OPEN_READ_ATTEMPTS):
            success, image = capture.read()
            if _is_usable(success, image):
                break
        else:
            capture.release()
            raise CameraError(
                f'La cámara {self.index} se abrió pero no entrega imágenes. '
                'Puede que otra aplicación la esté usando.'
            )

        self._capture = capture
        self._start_time = time.monotonic()
        self._last_timestamp_ms = -1

    def read(self):
        """Devuelve el siguiente `Frame`. Bloquea hasta que la cámara lo entrega."""
        if not self.is_open:
            raise CameraError('La cámara no está abierta.')

        success, image = self._capture.read()
        if not _is_usable(success, image):
            raise CameraError(f'La cámara {self.index} dejó de entregar imágenes.')

        return Frame(image=image, timestamp_ms=self._next_timestamp_ms())

    def close(self):
        """Libera la cámara. Se puede llamar aunque ya esté cerrada."""
        if self.is_open:
            self._capture.release()
            self._capture = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _next_timestamp_ms(self):
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        # Dos fotogramas pueden caer en el mismo milisegundo: siempre se avanza.
        self._last_timestamp_ms = max(elapsed_ms, self._last_timestamp_ms + 1)
        return self._last_timestamp_ms


def _is_usable(success, image):
    return bool(success) and image is not None and image.size > 0
