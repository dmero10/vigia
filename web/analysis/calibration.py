"""M2 - Lógica: calibración del umbral de EAR por usuario (RF-04, RNF-03)."""

import statistics
from dataclasses import dataclass

CALIBRATION_MS = 5000
MIN_SAMPLES = 30
THRESHOLD_FACTOR = 0.75
MIN_THRESHOLD = 0.12
MAX_THRESHOLD = 0.30


class CalibrationError(Exception):
    """La calibración terminó sin muestras suficientes."""


@dataclass(frozen=True)
class CalibrationResult:
    threshold: float
    median_ear: float
    samples: int


class EarCalibrator:
    """Reúne el EAR con los ojos abiertos durante 5 s y calcula el umbral."""

    def __init__(self, duration_ms=CALIBRATION_MS, min_samples=MIN_SAMPLES):
        self.duration_ms = duration_ms
        self.min_samples = min_samples
        self._samples = []
        self._start_ms = None
        self._last_ms = None

    def update(self, timestamp_ms, ear=None):
        """Registra un fotograma. `ear=None` si ese fotograma no se pudo medir.

        El tiempo corre aunque no haya rostro: así la calibración siempre termina
        y puede informar que faltaron muestras, en lugar de quedarse esperando.
        """
        if self._start_ms is None:
            self._start_ms = timestamp_ms
        self._last_ms = timestamp_ms
        if ear is not None and not self.is_finished:
            self._samples.append(ear)

    @property
    def is_finished(self):
        if self._start_ms is None:
            return False
        return self._last_ms - self._start_ms >= self.duration_ms

    def result(self):
        """Calcula el umbral: mediana(EAR) x 0.75, limitado entre 0.12 y 0.30."""
        if len(self._samples) < self.min_samples:
            raise CalibrationError(
                f'Calibración incompleta: {len(self._samples)} muestras de '
                f'{self.min_samples} necesarias. Mire a la cámara con buena luz y repita.'
            )
        median_ear = statistics.median(self._samples)
        threshold = min(MAX_THRESHOLD, max(MIN_THRESHOLD, median_ear * THRESHOLD_FACTOR))
        return CalibrationResult(threshold, median_ear, len(self._samples))
