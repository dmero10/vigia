"""Orquestador: une cámara (M1), análisis (M2) y pulsera (M3) en un hilo propio.

Reglas de CLAUDE.md:
- Corre en segundo plano, nunca dentro de una vista HTTP. Las vistas sólo llaman
  a start(), stop(), send_command(), state(), summary() y latest_jpeg().
- Se inicia al pulsar «Iniciar monitoreo», no al arrancar Django.
- RNF-05: la imagen de vista previa sólo existe en memoria; nunca se guarda.

Recorrido (igual que en el Figma):
  starting -> preview (Conectar cámara / Conectar pulsera)
           -> calibrating -> calibrated («Tu mirada, calibrada»)
           -> monitoring -> finished
En preview y calibrated el hilo sigue leyendo la cámara y espera un botón.
"""

import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

import cv2
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from acquisition.camera import Camera, CameraError
from acquisition.validation import FpsMeter, has_enough_light, mean_brightness
from alerts.services import AlertSender, AlertStatus, create_bracelet
from analysis.calibration import CALIBRATION_MS, CalibrationError, EarCalibrator
from analysis.classifier import DrowsinessClassifier
from analysis.landmarks import FaceLandmarkDetector, LandmarkError
from analysis.metrics import compute_ear, compute_mar
from monitoring.registry_client import NullRegistry, get_registry_queue

logger = logging.getLogger(__name__)

PHASE_IDLE = 'idle'
PHASE_STARTING = 'starting'  # Abriendo cámara, modelo y pulsera.
PHASE_PREVIEW = 'preview'  # Cámara lista; espera «calibrate».
PHASE_CALIBRATING = 'calibrating'
PHASE_CALIBRATED = 'calibrated'  # Espera «monitor» o «recalibrate».
PHASE_MONITORING = 'monitoring'
PHASE_FINISHED = 'finished'
PHASE_ERROR = 'error'

COMMAND_CALIBRATE = 'calibrate'
COMMAND_RECALIBRATE = 'recalibrate'
COMMAND_MONITOR = 'monitor'
COMMANDS = (COMMAND_CALIBRATE, COMMAND_RECALIBRATE, COMMAND_MONITOR)

PREVIEW_EVERY_N_FRAMES = 2  # La vista previa va a ~15 FPS para no restar FPS al análisis.
PREVIEW_JPEG_QUALITY = 70
STOP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class LevelEvent:
    """RF-11: cada cambio de nivel o de medición, con su hora."""

    at: datetime
    level: int
    degraded_reason: str | None
    ear: float | None = None
    mar: float | None = None
    closed_ms: int = 0
    yawn_count: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def as_payload(self):
        """Datos que se envían a M4 (sólo métricas, nunca imágenes)."""
        return asdict(self)


@dataclass(frozen=True)
class SessionSummary:
    """RF-12: resumen de la sesión."""

    started_at: datetime
    ended_at: datetime
    duration_s: float
    alert_count: int  # Veces que se pasó de nivel 0 a una alerta (1-3).
    max_level: int
    threshold: float | None = None
    events: tuple = ()
    session_id: str | None = None  # Identificador de la sesión en M4.


@dataclass(frozen=True)
class MonitoringState:
    """Foto del estado actual, para las pantallas."""

    phase: str = PHASE_IDLE
    error: str | None = None
    notice: str | None = None  # Aviso no fatal, p. ej. una calibración fallida.
    calibration_progress: float = 0.0  # De 0 a 1.
    threshold: float | None = None
    face_detected: bool = False
    light_ok: bool = False
    level: int = 0
    ear: float | None = None
    mar: float | None = None
    yawn_count: int = 0
    closed_ms: int = 0
    degraded_reason: str | None = None
    fps: float = 0.0
    elapsed_s: float = 0.0
    alert_count: int = 0
    max_level: int = 0
    bracelet: AlertStatus | None = None
    bracelet_name: str | None = None
    bracelet_simulated: bool = False

    def as_dict(self):
        return asdict(self)


@dataclass
class _Measurement:
    timestamp_ms: int
    ear: float | None
    mar: float | None
    light_ok: bool


@dataclass
class _Session:
    started_at: datetime
    started_monotonic: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    alert_count: int = 0
    max_level: int = 0
    last_level: int = 0
    last_degraded: str | None = None
    events: list = field(default_factory=list)


class Orchestrator:
    """Una sesión de monitoreo a la vez."""

    def __init__(self, camera_factory=Camera, detector_factory=FaceLandmarkDetector,
                 bracelet_factory=create_bracelet, registry=None):
        self._camera_factory = camera_factory
        self._detector_factory = detector_factory
        self._bracelet_factory = bracelet_factory
        # M4: cola de envío que nunca bloquea (ver registry_client.py).
        self._registry = registry if registry is not None else NullRegistry()
        self._bracelet_mode = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._command = None
        self._state = MonitoringState()
        self._session = None
        self._summary = None
        self._sender = None
        self._preview = None
        self._fps = FpsMeter()

    # ---------- Lo que usan las vistas ----------

    def start(self, bracelet_mode=None):
        """Inicia una sesión. Devuelve False si ya había una en curso.

        `bracelet_mode`: 'simulated' o 'ble'; None usa `settings.BRACELET_MODE`.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._bracelet_mode = bracelet_mode
            self._stop_event.clear()
            self._command = None
            self._session = _Session(timezone.now(), time.monotonic())
            self._summary = None
            self._sender = None
            self._preview = None
            self._fps = FpsMeter()
            self._state = MonitoringState(phase=PHASE_STARTING)
            session = self._session
            self._thread = threading.Thread(target=self._run, name='monitoring', daemon=True)
            self._thread.start()
        self._registry.start_session(
            session.id, session.started_at, bracelet_mode or settings.BRACELET_MODE
        )
        return True

    def stop(self):
        """Termina la sesión y espera a que se liberen la cámara y la pulsera."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(STOP_TIMEOUT_S)

    def send_command(self, command):
        """Botones del recorrido: 'calibrate', 'recalibrate' o 'monitor'."""
        if command not in COMMANDS:
            raise ValueError(f'Comando desconocido: {command}')
        with self._lock:
            self._command = command

    def retry_bracelet(self):
        """Botón «Reconectar pulsera»: reintenta ya, sin esperar el próximo intento."""
        with self._lock:
            sender = self._sender
        if sender is not None:
            sender.retry_now()

    @property
    def is_running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def bracelet(self):
        """Pulsera de la sesión actual (o None)."""
        with self._lock:
            sender = self._sender
        return sender.bracelet if sender is not None else None

    def state(self):
        with self._lock:
            state = self._state
            sender = self._sender
            session = self._session
        updates = {}
        if sender is not None:
            updates['bracelet'] = sender.status()
            updates['bracelet_name'] = sender.bracelet.name
            updates['bracelet_simulated'] = hasattr(sender.bracelet, 'set_available')
        if session is not None and state.phase not in (PHASE_IDLE, PHASE_FINISHED, PHASE_ERROR):
            updates['elapsed_s'] = time.monotonic() - session.started_monotonic
        return _replace(state, **updates)

    def summary(self):
        with self._lock:
            return self._summary

    def latest_jpeg(self):
        """Último fotograma en JPEG (sólo en memoria), o None si no hay sesión."""
        with self._lock:
            return self._preview

    # ---------- Hilo en segundo plano ----------

    def _run(self):
        sender = None
        try:
            # La pulsera se conecta primero: así tiene tiempo mientras se ve la cámara.
            sender = AlertSender(self._bracelet_factory(self._bracelet_mode))
            sender.start()
            with self._lock:
                self._sender = sender
            with self._detector_factory() as detector, self._camera_factory() as camera:
                threshold = self._prepare(camera, detector)
                if threshold is not None:
                    self._monitor(camera, detector, sender, threshold)
        except (CameraError, LandmarkError, ImproperlyConfigured) as error:
            self._fail(str(error))
        except Exception as error:  # noqa: BLE001 - el hilo nunca debe morir en silencio
            logger.exception('Error inesperado en el monitoreo')
            self._fail(f'Error inesperado: {error}')
        finally:
            if sender is not None:
                sender.stop()  # Envía nivel 0 y desconecta la pulsera.
            self._finish()

    def _prepare(self, camera, detector):
        """Cámara -> calibración -> confirmación. Devuelve el umbral o None si se detuvo."""
        self._set_state(phase=PHASE_PREVIEW)
        command = self._wait_for(camera, detector, {COMMAND_CALIBRATE})
        while command is not None:
            try:
                threshold = self._calibrate(camera, detector)
            except CalibrationError as error:
                # Se vuelve a la cámara con el aviso, para poder intentarlo otra vez.
                self._set_state(phase=PHASE_PREVIEW, notice=str(error))
                command = self._wait_for(camera, detector, {COMMAND_CALIBRATE})
                continue
            if threshold is None:
                return None
            self._set_state(phase=PHASE_CALIBRATED, threshold=threshold)
            command = self._wait_for(camera, detector, {COMMAND_MONITOR, COMMAND_RECALIBRATE})
            if command == COMMAND_MONITOR:
                return threshold
        return None

    def _wait_for(self, camera, detector, allowed):
        """Sigue mostrando la cámara hasta recibir un comando permitido o detenerse."""
        frame_index = 0
        while not self._stop_event.is_set():
            with self._lock:
                command, self._command = self._command, None
            if command in allowed:
                return command
            measurement = self._measure(camera, detector, frame_index)
            frame_index += 1
            self._show(measurement)
        return None

    def _calibrate(self, camera, detector):
        self._set_state(phase=PHASE_CALIBRATING, calibration_progress=0.0, notice=None)
        calibrator = EarCalibrator()
        first_ms = None
        frame_index = 0
        while not calibrator.is_finished:
            if self._stop_event.is_set():
                return None
            measurement = self._measure(camera, detector, frame_index)
            frame_index += 1
            first_ms = measurement.timestamp_ms if first_ms is None else first_ms
            ear = measurement.ear if measurement.light_ok else None
            calibrator.update(measurement.timestamp_ms, ear)
            progress = min(1.0, (measurement.timestamp_ms - first_ms) / CALIBRATION_MS)
            self._show(measurement, calibration_progress=progress)
        return calibrator.result().threshold

    def _monitor(self, camera, detector, sender, threshold):
        self._set_state(phase=PHASE_MONITORING)
        classifier = DrowsinessClassifier(threshold)
        frame_index = 0
        while not self._stop_event.is_set():
            measurement = self._measure(camera, detector, frame_index)
            frame_index += 1
            assessment = classifier.update(
                measurement.timestamp_ms, measurement.ear, measurement.mar, measurement.light_ok
            )
            sender.set_level(assessment.level)  # No bloquea: lo envía el hilo de la pulsera.
            self._record(assessment, measurement)
            with self._lock:
                session = self._session
                alert_count, max_level = session.alert_count, session.max_level
            self._show(
                measurement,
                level=assessment.level,
                yawn_count=assessment.yawn_count,
                closed_ms=assessment.closed_ms,
                degraded_reason=assessment.degraded_reason,
                alert_count=alert_count,
                max_level=max_level,
            )

    def _measure(self, camera, detector, frame_index):
        frame = camera.read()
        self._fps.tick()
        light_ok = has_enough_light(mean_brightness(frame.image))
        landmarks = detector.detect(frame.image, frame.timestamp_ms)
        if frame_index % PREVIEW_EVERY_N_FRAMES == 0:
            self._publish_preview(frame.image)
        if landmarks is None:
            return _Measurement(frame.timestamp_ms, None, None, light_ok)
        return _Measurement(
            frame.timestamp_ms, compute_ear(landmarks), compute_mar(landmarks), light_ok
        )

    def _show(self, measurement, **changes):
        self._set_state(
            ear=measurement.ear,
            mar=measurement.mar,
            face_detected=measurement.ear is not None,
            light_ok=measurement.light_ok,
            fps=self._fps.fps,
            **changes,
        )

    def _publish_preview(self, image):
        # Se refleja como un espejo, que es lo natural al verse en la pantalla.
        ok, encoded = cv2.imencode(
            '.jpg', cv2.flip(image, 1), [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY]
        )
        if ok:
            with self._lock:
                self._preview = encoded.tobytes()

    def _record(self, assessment, measurement):
        """RF-11: guarda cada cambio de nivel o de medición y lo encola para M4."""
        level, degraded_reason = assessment.level, assessment.degraded_reason
        with self._lock:
            session = self._session
            if level == session.last_level and degraded_reason == session.last_degraded:
                return
            if session.last_level == 0 and level > 0:
                session.alert_count += 1
            session.max_level = max(session.max_level, level)
            session.last_level = level
            session.last_degraded = degraded_reason
            event = LevelEvent(
                at=timezone.now(), level=level, degraded_reason=degraded_reason,
                ear=_rounded(measurement.ear), mar=_rounded(measurement.mar),
                closed_ms=assessment.closed_ms, yawn_count=assessment.yawn_count,
            )
            session.events.append(event)
        self._registry.add_event(session.id, event.as_payload())  # Sólo encola.

    def _set_state(self, **changes):
        with self._lock:
            self._state = _replace(self._state, **changes)

    def _fail(self, message):
        self._set_state(phase=PHASE_ERROR, error=message)

    def _finish(self):
        with self._lock:
            session = self._session
            duration_s = time.monotonic() - session.started_monotonic
            self._summary = SessionSummary(
                started_at=session.started_at,
                ended_at=timezone.now(),
                duration_s=duration_s,
                alert_count=session.alert_count,
                max_level=session.max_level,
                threshold=self._state.threshold,
                events=tuple(session.events),
                session_id=session.id,
            )
            summary = self._summary
            phase = PHASE_ERROR if self._state.phase == PHASE_ERROR else PHASE_FINISHED
            self._state = _replace(self._state, phase=phase, level=0, elapsed_s=duration_s)
            self._preview = None
        self._registry.finish_session(
            summary.session_id, summary.ended_at, _rounded(summary.threshold)
        )


def _rounded(value):
    return None if value is None else round(float(value), 4)


def _replace(state, **changes):
    values = {name: getattr(state, name) for name in state.__dataclass_fields__}
    values.update(changes)
    return MonitoringState(**values)


_orchestrator = None
_orchestrator_lock = threading.Lock()


def get_orchestrator():
    """El orquestador único del proceso (sólo hay una cámara)."""
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is None:
            _orchestrator = Orchestrator(registry=get_registry_queue())
        return _orchestrator
