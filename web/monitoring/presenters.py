"""Datos listos para mostrar: lo que reciben las pantallas por WebSocket o JSON.

Lo usan `views.py` (HTTP) y `consumers.py` (WebSocket), para que los dos envíen
exactamente lo mismo. El texto de cada registro («Empieza una alerta…») sale de
`explain.py`, el mismo que usa M4 (y que arma el CSV).
"""

from dataclasses import asdict
from datetime import datetime

from django.utils import timezone

from monitoring.explain import LEVEL_NAMES, STILL_ACTIVE, explain, format_seconds
from monitoring.orchestrator import RNF02_MIN_FPS, RNF02_MIN_MONITORING_S

STRENGTH_NAMES = {1: 'Muy suave', 2: 'Suave', 3: 'Media', 4: 'Fuerte', 5: 'Máxima'}


def local_event(event):
    """Un LevelEvent del orquestador como dict para `explain`."""
    return {'at': event.at, 'level': event.level, 'degraded_reason': event.degraded_reason,
            'closed_ms': event.closed_ms, 'yawn_count': event.yawn_count,
            'ear': event.ear, 'mar': event.mar}


def remote_event(event):
    """Un evento recibido de M4 (JSON) como dict para `explain`."""
    return {'at': datetime.fromisoformat(event['at']), 'level': event['level'],
            'degraded_reason': event.get('degraded_reason'),
            'closed_ms': event.get('closed_ms') or 0, 'yawn_count': event.get('yawn_count') or 0,
            'ear': event.get('ear'), 'mar': event.get('mar')}


def explained(events, ended_at=None, live=False):
    """(registro, alertas) con los textos que muestra la pantalla.
    `live`: la sesión sigue, así que una alerta abierta está «En curso»."""
    log, alerts = explain(events, ended_at)
    log_rows = [{
        'time': timezone.localtime(row['at']).strftime('%H:%M:%S'),
        'date_time': timezone.localtime(row['at']).strftime('%Y-%m-%d %H:%M:%S'),
        'level': row['level'],
        'pill': 'deg' if row['degraded'] else (f"l{row['level']}" if row['level'] else ''),
        'event': row['event'],
        'detail': row['detail'],
    } for row in log]
    alert_rows = [{
        'number': alert['number'],
        'time': timezone.localtime(alert['start']).strftime('%H:%M:%S'),
        'duration': '–' if alert['duration_s'] is None else format_seconds(alert['duration_s']),
        'max_level': alert['max_level'],
        'max_level_name': alert['max_level_name'],
        'cause': alert['cause'],
        'ended_by': 'En curso' if live and alert['ended_by'] == STILL_ACTIVE else alert['ended_by'],
    } for alert in alerts]
    return log_rows, alert_rows


def format_duration(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def rnf02_text(monitoring_s, fps_min):
    """Texto de la prueba de 5 minutos (RNF-02) para el resumen."""
    if fps_min is None:
        return 'Prueba de 5 min (RNF-02): sin datos de FPS.'
    if fps_min < RNF02_MIN_FPS:
        return f'Prueba de 5 min (RNF-02): no cumplida, el FPS bajó a {fps_min:.0f}.'
    if monitoring_s < RNF02_MIN_MONITORING_S:
        missing = (RNF02_MIN_MONITORING_S - monitoring_s) / 60
        return f'Prueba de 5 min (RNF-02): en curso, faltan {missing:.1f} min de monitoreo.'
    return 'Prueba de 5 min (RNF-02): cumplida.'


def summary_payload(summary, live=False):
    """Resumen (final o en vivo) con los textos que muestra la pantalla."""
    log, alerts = explained([local_event(event) for event in summary.events], summary.ended_at,
                            live=live)
    return {
        'duration': format_duration(summary.duration_s),
        'alert_count': summary.alert_count,
        'max_level': summary.max_level,
        'max_level_name': LEVEL_NAMES[summary.max_level],
        'threshold': summary.threshold,
        'monitoring_s': summary.monitoring_s,
        'fps_avg': summary.fps_avg,
        'fps_min': summary.fps_min,
        'rnf02': rnf02_text(summary.monitoring_s, summary.fps_min),
        'log': log,
        'alerts': alerts,
    }


def live_payload(orchestrator, registry_queue):
    """Todo lo que la página necesita en un instante: estado, pulsera, M4 y resumen en vivo."""
    data = orchestrator.state().as_dict()
    data['running'] = orchestrator.is_running
    data['registry'] = asdict(registry_queue.status())  # Cola de envío hacia M4.
    live = orchestrator.live_summary()
    data['summary'] = None if live is None else summary_payload(live, live=True)
    return data
