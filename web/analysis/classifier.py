"""M2 - Lógica: decide el nivel de alerta en cada fotograma (RF-02, RF-05, RF-06).

Todos los tiempos salen de `timestamp_ms`, no del reloj, para poder probar el
comportamiento sin esperar segundos reales.
"""

from collections import deque
from dataclasses import dataclass

# Cierre ocular continuo: (milisegundos, nivel), de mayor a menor. Valores ajustables.
CLOSURE_LEVELS = ((5000, 3), (3000, 2), (1500, 1))
EYES_OPEN_END_MS = 200  # Ojos abiertos este tiempo = fin del cierre (evita ruido).

YAWN_MAR = 0.6
YAWN_MIN_MS = 1000
YAWNS_FOR_ALERT = 3
YAWN_WINDOW_MS = 5 * 60 * 1000
YAWN_ALERT_MS = 3000
YAWN_ALERT_LEVEL = 1

# Con la boca muy abierta la cara se estira y el EAR baja aunque los ojos sigan
# abiertos. Mientras dure y un rato después, se exige un cierre más marcado.
# Medido con una persona: ojos abiertos con la boca abierta, EAR 0.16-0.23;
# ojos cerrados, EAR 0.04. Valores ajustables.
MOUTH_OPEN_MAR = 0.5
MOUTH_OPEN_EAR_FACTOR = 0.6
MOUTH_RECOVERY_MS = 3000

NO_FACE_LIMIT_MS = 500

DEGRADED_LOW_LIGHT = 'low_light'
DEGRADED_NO_FACE = 'no_face'


@dataclass(frozen=True)
class Assessment:
    """Resultado de analizar un fotograma."""

    level: int
    degraded_reason: str | None
    closed_ms: int
    yawn_count: int

    @property
    def is_degraded(self):
        return self.degraded_reason is not None


class DrowsinessClassifier:
    """Máquina de estados que se alimenta con un fotograma a la vez."""

    def __init__(self, threshold):
        self.threshold = threshold
        self._closure_start_ms = None
        self._open_since_ms = None
        self._no_face_since_ms = None
        self._yawn_start_ms = None
        self._yawn_counted = False
        self._yawn_times = deque()
        self._yawn_alert_until_ms = None
        self._mouth_recovery_until_ms = None

    def update(self, timestamp_ms, ear=None, mar=None, light_ok=True):
        """Procesa un fotograma. `ear` y `mar` son None si no se detectó un rostro."""
        self._forget_old_yawns(timestamp_ms)
        has_face = ear is not None and mar is not None

        if has_face:
            self._no_face_since_ms = None
        elif self._no_face_since_ms is None:
            self._no_face_since_ms = timestamp_ms

        if not light_ok:
            return self._degrade(DEGRADED_LOW_LIGHT)

        if not has_face:
            if timestamp_ms - self._no_face_since_ms > NO_FACE_LIMIT_MS:
                return self._degrade(DEGRADED_NO_FACE)
            # Pérdida breve del rostro: se conserva el estado hasta superar el límite.
            return self._assess(timestamp_ms)

        self._update_closure(timestamp_ms, ear, self._closed_eye_threshold(timestamp_ms, mar))
        self._update_yawn(timestamp_ms, mar)
        return self._assess(timestamp_ms)

    def _closed_eye_threshold(self, timestamp_ms, mar):
        if mar > MOUTH_OPEN_MAR:
            self._mouth_recovery_until_ms = timestamp_ms + MOUTH_RECOVERY_MS
        if self._mouth_recovery_until_ms is not None and timestamp_ms < self._mouth_recovery_until_ms:
            return self.threshold * MOUTH_OPEN_EAR_FACTOR
        return self.threshold

    def _update_closure(self, timestamp_ms, ear, threshold):
        if ear < threshold:
            if self._closure_start_ms is None:
                self._closure_start_ms = timestamp_ms
            self._open_since_ms = None
        elif self._closure_start_ms is not None:
            if self._open_since_ms is None:
                self._open_since_ms = timestamp_ms
            if timestamp_ms - self._open_since_ms >= EYES_OPEN_END_MS:
                self._reset_closure()

    def _update_yawn(self, timestamp_ms, mar):
        if mar <= YAWN_MAR:
            self._reset_yawn()
            return

        if self._yawn_start_ms is None:
            self._yawn_start_ms = timestamp_ms

        # Cada apertura de boca cuenta una sola vez, aunque dure mucho.
        if not self._yawn_counted and timestamp_ms - self._yawn_start_ms >= YAWN_MIN_MS:
            self._yawn_counted = True
            self._yawn_times.append(timestamp_ms)
            if len(self._yawn_times) >= YAWNS_FOR_ALERT:
                self._yawn_alert_until_ms = timestamp_ms + YAWN_ALERT_MS
                self._yawn_times.clear()

    def _forget_old_yawns(self, timestamp_ms):
        while self._yawn_times and timestamp_ms - self._yawn_times[0] > YAWN_WINDOW_MS:
            self._yawn_times.popleft()

    def _reset_closure(self):
        self._closure_start_ms = None
        self._open_since_ms = None

    def _reset_yawn(self):
        self._yawn_start_ms = None
        self._yawn_counted = False

    def _degrade(self, reason):
        # RF-02: se envía nivel 0 y no se mantienen alertas anteriores.
        # Los bostezos ya contados se conservan: la ventana de 5 min sigue vigente.
        self._reset_closure()
        self._reset_yawn()
        self._yawn_alert_until_ms = None
        self._mouth_recovery_until_ms = None
        return Assessment(0, reason, 0, len(self._yawn_times))

    def _assess(self, timestamp_ms):
        closed_ms = 0
        if self._closure_start_ms is not None:
            closed_ms = timestamp_ms - self._closure_start_ms

        level = _closure_level(closed_ms)
        if self._yawn_alert_until_ms is not None and timestamp_ms < self._yawn_alert_until_ms:
            level = max(level, YAWN_ALERT_LEVEL)

        return Assessment(level, None, closed_ms, len(self._yawn_times))


def _closure_level(closed_ms):
    for minimum_ms, level in CLOSURE_LEVELS:
        if closed_ms >= minimum_ms:
            return level
    return 0
