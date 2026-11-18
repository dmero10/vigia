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
from alerts.bracelet import DEFAULT_STRENGTH, STRENGTHS
from alerts.services import AlertSender, AlertStatus, create_bracelet
from analysis.calibration import CALIBRATION_MS, CalibrationError, EarCalibrator
from analysis.classifier import DrowsinessClassifier
from analysis.landmarks import FaceLandmarkDetector, LandmarkError, box_area, face_box
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

# Varios rostros: se mide el más grande. Para no saltar de una persona a otra, se
# sigue midiendo el mismo mientras no aparezca otro claramente más grande.
SWITCH_FACE_RATIO = 1.5
MAIN_FACE_COLOR = (79, 125, 46)  # BGR del verde de la interfaz (#2e7d4f).
OTHER_FACE_COLOR = (150, 150, 150)
STOP_TIMEOUT_S = 10.0

# RNF-02: el procesamiento debe mantenerse fluido en sesiones de al menos 5 minutos.
RNF02_MIN_MONITORING_S = 300.0
RNF02_MIN_FPS = 25.0
FPS_WARMUP_S = 3.0  # Los primeros segundos no cuentan para el mínimo (la ventana se llena).


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
    monitoring_s: float = 0.0  # Tiempo en monitoreo activo (sin preparación ni calibración).
    fps_avg: float | None = None  # FPS promedio durante el monitoreo.
    fps_min: float | None = None  # Peor FPS de la ventana de 2 s, tras el arranque.

    @property
    def rnf02_passed(self):
        """RNF-02: al menos 5 min de monitoreo sin bajar de 25 FPS."""
        return (self.monitoring_s >= RNF02_MIN_MONITORING_S and self.fps_min is not None
                and self.fps_min >= RNF02_MIN_FPS)


@dataclass(frozen=True)
class MonitoringState:
    """Foto del estado actual, para las pantallas."""

    phase: str = PHASE_IDLE
    error: str | None = None
    notice: str | None = None  # Aviso no fatal, p. ej. una calibración fallida.
    calibration_progress: float = 0.0  # De 0 a 1.
    threshold: float | None = None
    face_detected: bool = False
    face_count: int = 0  # Rostros en la imagen; se mide sólo uno (recuadro verde).
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
    vibration_strength: int = DEFAULT_STRENGTH  # 1-5, la elige el usuario.
    monitoring_s: float = 0.0
    fps_avg: float | None = None
    fps_min: float | None = None

    def as_dict(self):
        return asdict(self)


@dataclass
class _Measurement:
    timestamp_ms: int
    ear: float | None
    mar: float | None
    light_ok: bool
    face_count: int = 0


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
    monitoring_started: float | None = None  # Reloj monótono al empezar el monitoreo.
    monitoring_frames: int = 0
    fps_min: float | None = None

    def monitoring_s(self, now):
        return 0.0 if self.monitoring_started is None else now - self.monitoring_started

    def fps_avg(self, now):
        elapsed = self.monitoring_s(now)
        if elapsed <= 0 or not self.monitoring_frames:
            return None
        return self.monitoring_frames / elapsed


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
        self._strength = DEFAULT_STRENGTH
        self._main_center = None  # Centro del rostro medido en el fotograma anterior.

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
            self._main_center = None
            self._state = MonitoringState(phase=PHASE_STARTING, vibration_strength=self._strength)
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

    def set_strength(self, strength):
        """Deslizador de fuerza (1-5): escala la vibración de todos los niveles.
        Sirve antes y durante la sesión; se envía a la pulsera al momento."""
        if strength not in STRENGTHS:
            raise ValueError(f'Fuerza inválida: {strength}. Debe estar entre 1 y 5.')
        with self._lock:
            self._strength = strength
            sender = self._sender
            self._state = _replace(self._state, vibration_strength=strength)
        if sender is not None:
            sender.set_strength(strength)

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
            now = time.monotonic()
            updates['elapsed_s'] = now - session.started_monotonic
            updates['monitoring_s'] = session.monitoring_s(now)
            updates['fps_avg'] = session.fps_avg(now)
            updates['fps_min'] = session.fps_min
        return _replace(state, **updates)

    def summary(self):
        """Resumen final, cuando la sesión ya terminó (o None)."""
        with self._lock:
            return self._summary

    def live_summary(self):
        """Resumen parcial mientras el monitoreo sigue (primer «Finalizar sesión»).
        Devuelve None si no hay una sesión en monitoreo."""
        if not self.is_running:
            return None
        with self._lock:
            session = self._session
            if session is None or session.monitoring_started is None:
                return None
            return self._build_summary(session, timezone.now())

    def latest_jpeg(self):
        """Último fotograma en JPEG (sólo en memoria), o None si no hay sesión."""
        with self._lock:
            return self._preview

    # ---------- Hilo en segundo plano ----------

    def _run(self):
        sender = None
        try:
            # La pulsera se conecta primero: así tiene tiempo mientras se ve la cámara.
            sender = AlertSender(
                self._bracelet_factory(self._bracelet_mode), strength=self._strength
            )
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
        with self._lock:
            self._session.monitoring_started = time.monotonic()
        while not self._stop_event.is_set():
            measurement = self._measure(camera, detector, frame_index)
            frame_index += 1
            self._track_fps()
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
        faces = _detect_faces(detector, frame.image, frame.timestamp_ms)
        main = self._pick_main_face(faces)
        if frame_index % PREVIEW_EVERY_N_FRAMES == 0:
            self._publish_preview(frame.image, faces, main)
        if main is None:
            return _Measurement(frame.timestamp_ms, None, None, light_ok, 0)
        landmarks = faces[main]
        return _Measurement(
            frame.timestamp_ms, compute_ear(landmarks), compute_mar(landmarks), light_ok,
            len(faces),
        )

    def _pick_main_face(self, faces):
        """Índice del rostro que se mide: el más grande, salvo que el que ya se medía
        siga ahí y no haya otro claramente más grande (evita saltar entre personas)."""
        if not faces:
            self._main_center = None
            return None
        boxes = [face_box(face) for face in faces]
        areas = [box_area(box) for box in boxes]
        largest = max(range(len(faces)), key=areas.__getitem__)
        main = largest
        if self._main_center is not None and len(faces) > 1:
            cx, cy = self._main_center
            nearest = min(range(len(faces)), key=lambda i: (
                ((boxes[i][0] + boxes[i][2]) / 2 - cx) ** 2
                + ((boxes[i][1] + boxes[i][3]) / 2 - cy) ** 2))
            if areas[largest] < areas[nearest] * SWITCH_FACE_RATIO:
                main = nearest
        x0, y0, x1, y1 = boxes[main]
        self._main_center = ((x0 + x1) / 2, (y0 + y1) / 2)
        return main

    def _show(self, measurement, **changes):
        self._set_state(
            ear=measurement.ear,
            mar=measurement.mar,
            face_detected=measurement.ear is not None,
            face_count=measurement.face_count,
            light_ok=measurement.light_ok,
            fps=self._fps.fps,
            **changes,
        )

    def _publish_preview(self, image, faces=(), main=None):
        # Se refleja como un espejo, que es lo natural al verse en la pantalla.
        # Los recuadros sólo se dibujan en esta copia en memoria (RNF-05).
        mirrored = cv2.flip(image, 1)
        width = mirrored.shape[1]
        for index, face in enumerate(faces):
            x0, y0, x1, y1 = face_box(face)
            left, right = int(width - x1), int(width - x0)  # Coordenadas en espejo.
            is_main = index == main
            color = MAIN_FACE_COLOR if is_main else OTHER_FACE_COLOR
            cv2.rectangle(mirrored, (left, int(y0)), (right, int(y1)), color, 2 if is_main else 1)
            if len(faces) > 1:
                label = 'Rostro medido' if is_main else 'No se mide'
                cv2.putText(mirrored, label, (left, max(14, int(y0) - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
        ok, encoded = cv2.imencode(
            '.jpg', mirrored, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY]
        )
        if ok:
            with self._lock:
                self._preview = encoded.tobytes()

    def _track_fps(self):
        """RNF-02: cuenta fotogramas y guarda el peor FPS tras el arranque."""
        fps = self._fps.fps
        with self._lock:
            session = self._session
            session.monitoring_frames += 1
            if session.monitoring_s(time.monotonic()) >= FPS_WARMUP_S:
                session.fps_min = fps if session.fps_min is None else min(session.fps_min, fps)

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

    def _build_summary(self, session, ended_at):
        """Arma el resumen. Llamar con el candado tomado."""
        now = time.monotonic()
        avg = session.fps_avg(now)
        return SessionSummary(
            started_at=session.started_at,
            ended_at=ended_at,
            duration_s=now - session.started_monotonic,
            alert_count=session.alert_count,
            max_level=session.max_level,
            threshold=self._state.threshold,
            events=tuple(session.events),
            session_id=session.id,
            monitoring_s=session.monitoring_s(now),
            fps_avg=None if avg is None else round(avg, 1),
            fps_min=None if session.fps_min is None else round(session.fps_min, 1),
        )

    def _finish(self):
        with self._lock:
            self._summary = self._build_summary(self._session, timezone.now())
            summary = self._summary
            phase = PHASE_ERROR if self._state.phase == PHASE_ERROR else PHASE_FINISHED
            self._state = _replace(self._state, phase=phase, level=0, elapsed_s=summary.duration_s)
            self._preview = None
        self._registry.finish_session(
            summary.session_id, summary.ended_at, _rounded(summary.threshold),
            stats={'monitoring_s': round(summary.monitoring_s, 1),
                   'fps_avg': summary.fps_avg, 'fps_min': summary.fps_min},
        )


def _detect_faces(detector, image, timestamp_ms):
    """Todos los rostros; con un detector que sólo da uno (pruebas), una lista de 0 o 1."""
    if hasattr(detector, 'detect_all'):
        return list(detector.detect_all(image, timestamp_ms))
    landmarks = detector.detect(image, timestamp_ms)
    return [] if landmarks is None else [landmarks]


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
