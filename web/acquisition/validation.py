"""M1 - Lógica: validación del video (RF-01 y parte de RF-02).

La condición "sin rostro" de RF-02 se evalúa en `analysis`, que es donde se
detecta el rostro. Aquí sólo se revisa lo que se sabe sin analizar la cara.
"""

import time
from collections import deque

import cv2

MIN_BRIGHTNESS = 40  # RF-02: con luz media menor (escala 0-255) la medición se degrada.
MIN_FPS = 25  # RF-01
FPS_WINDOW_SECONDS = 2.0


def mean_brightness(image):
    """Luz media de una imagen BGR, en escala de 0 a 255."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(gray.mean())


def has_enough_light(brightness):
    return brightness >= MIN_BRIGHTNESS


def meets_min_fps(fps):
    return fps >= MIN_FPS


class FpsMeter:
    """Mide los FPS reales sobre una ventana deslizante de tiempo."""

    def __init__(self, window_seconds=FPS_WINDOW_SECONDS):
        self.window_seconds = window_seconds
        self._times = deque()

    def tick(self, now=None):
        """Registra un fotograma. `now` son segundos de un reloj monótono."""
        now = time.monotonic() if now is None else now
        self._times.append(now)
        while now - self._times[0] > self.window_seconds:
            self._times.popleft()

    @property
    def fps(self):
        """FPS medidos. Vale 0.0 si aún no hay tiempo transcurrido que medir."""
        # En Windows el reloj avanza a saltos: dos fotogramas pueden tener la misma hora.
        elapsed = self._times[-1] - self._times[0] if self._times else 0.0
        if elapsed <= 0:
            return 0.0
        return (len(self._times) - 1) / elapsed
