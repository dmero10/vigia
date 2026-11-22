"""Capa de lógica: gestión de sesiones, resumen (RF-12) y exportación CSV (RF-11).

No sabe nada de HTTP ni de SQL: recibe datos validados y usa el repositorio.
"""

from datetime import datetime, timezone

from app.explain import RNF02_MIN_FPS, RNF02_MIN_MONITORING_S, build_csv, explain
from app.schemas import BatchResult, EventOut, SessionOut, SummaryOut

LEVEL_NAMES = {0: 'Sin indicios', 1: 'Señal inicial', 2: 'Atención reducida', 3: 'Riesgo alto'}
DEGRADED_NAMES = {'low_light': 'Poca luz', 'no_face': 'Rostro no detectado'}


class SessionNotFound(Exception):
    pass


def event_status(level, degraded_reason):
    if degraded_reason:
        return f'Medición no disponible ({DEGRADED_NAMES.get(degraded_reason, degraded_reason)})'
    return LEVEL_NAMES[level]


def compute_summary(session, events, now=None):
    """Una alerta se cuenta cada vez que el nivel pasa de 0 a 1, 2 o 3."""
    alert_count = 0
    previous = 0
    for event in events:
        if previous == 0 and event['level'] > 0:
            alert_count += 1
        previous = event['level']
    end = session['ended_at'] or now or datetime.now(timezone.utc)
    max_level = max((event['level'] for event in events), default=0)
    monitoring_s, fps_min = session.get('monitoring_s'), session.get('fps_min')
    rnf02 = None
    if monitoring_s is not None and fps_min is not None:
        rnf02 = monitoring_s >= RNF02_MIN_MONITORING_S and fps_min >= RNF02_MIN_FPS
    return SummaryOut(
        session_id=session['id'],
        started_at=session['started_at'],
        ended_at=session['ended_at'],
        finished=session['ended_at'] is not None,
        duration_s=max(0.0, (end - session['started_at']).total_seconds()),
        alert_count=alert_count,
        max_level=max_level,
        max_level_name=LEVEL_NAMES[max_level],
        threshold=session['threshold'],
        event_count=len(events),
        monitoring_s=monitoring_s,
        fps_avg=session.get('fps_avg'),
        fps_min=fps_min,
        rnf02_passed=rnf02,
    )


class RegistryService:
    def __init__(self, repository, time_zone):
        self.repository = repository
        self.time_zone = time_zone

    def create_session(self, data):
        """Devuelve (sesión, creada). Si ya existía, no se modifica."""
        created = self.repository.insert_session(data.id, data.started_at, data.bracelet_mode)
        return self.get_session(data.id), created

    def finish_session(self, session_id, data):
        finished = self.repository.finish_session(
            session_id, data.ended_at, data.threshold,
            monitoring_s=data.monitoring_s, fps_avg=data.fps_avg, fps_min=data.fps_min,
        )
        if not finished:
            raise SessionNotFound(session_id)
        return self.get_session(session_id)

    def add_events(self, session_id, batch):
        self._require(session_id)
        accepted, duplicates = self.repository.insert_events(session_id, batch.events)
        return BatchResult(accepted=accepted, duplicates=duplicates)

    def get_session(self, session_id):
        return _session_out(self._require(session_id))

    def list_sessions(self, limit=50):
        return [_session_out(row) for row in self.repository.list_sessions(limit)]

    def list_events(self, session_id):
        session = self._require(session_id)
        rows = self.repository.list_events(session_id)
        log, _ = explain(rows, session['ended_at'])
        return [
            EventOut(**row, status=event_status(row['level'], row['degraded_reason']),
                     event=text['event'], detail=text['detail'])
            for row, text in zip(rows, log)
        ]

    def summary(self, session_id):
        session = self._require(session_id)
        return compute_summary(session, self.repository.list_events(session_id))

    def export_csv(self, session_id):
        """CSV de la sesión (RF-11): resumen, alertas, registro detallado y cómo
        leerlo. Sólo métricas, nunca imágenes."""
        session = self._require(session_id)
        events = self.repository.list_events(session_id)
        summary = compute_summary(session, events)
        local = self._local
        return build_csv(
            {**summary.model_dump(), 'started_at': local(summary.started_at),
             'ended_at': local(summary.ended_at), 'bracelet_mode': session['bracelet_mode']},
            [{**event, 'at': local(event['at'])} for event in events],
        )

    def _local(self, moment):
        return None if moment is None else moment.astimezone(self.time_zone)

    def csv_filename(self, session_id):
        started = self._require(session_id)['started_at'].astimezone(self.time_zone)
        return f'vigia_sesion_{started:%Y%m%d_%H%M}.csv'

    def _require(self, session_id):
        session = self.repository.get_session(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session


def _session_out(row):
    return SessionOut(**row, status='finished' if row['ended_at'] else 'open')

