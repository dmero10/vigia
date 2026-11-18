"""Datos listos para mostrar: lo que reciben las pantallas por WebSocket o JSON.

Lo usan `views.py` (HTTP) y `consumers.py` (WebSocket), para que los dos envíen
exactamente lo mismo.
"""

from dataclasses import asdict

from django.utils import timezone

from monitoring.orchestrator import RNF02_MIN_FPS, RNF02_MIN_MONITORING_S

LEVEL_NAMES = {0: 'Sin indicios', 1: 'Señal inicial', 2: 'Atención reducida', 3: 'Riesgo alto'}
DEGRADED_NAMES = {'low_light': 'Poca luz', 'no_face': 'Rostro no detectado'}
STRENGTH_NAMES = {1: 'Muy suave', 2: 'Suave', 3: 'Media', 4: 'Fuerte', 5: 'Máxima'}


def event_row(event):
    """Un evento (LevelEvent) como fila de la tabla y del CSV."""
    if event.degraded_reason:
        name = DEGRADED_NAMES.get(event.degraded_reason, event.degraded_reason)
        status = f'Medición no disponible ({name})'
    else:
        status = LEVEL_NAMES[event.level]
    return {
        'time': timezone.localtime(event.at).strftime('%Y-%m-%d %H:%M:%S'),
        'level': event.level,
        'status': status,
    }


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


def summary_payload(summary):
    """Resumen (final o en vivo) con los textos que muestra la pantalla."""
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
        'events': [event_row(event) for event in summary.events],
    }


def live_payload(orchestrator, registry_queue):
    """Todo lo que la página necesita en un instante: estado, pulsera, M4 y resumen en vivo."""
    data = orchestrator.state().as_dict()
    data['running'] = orchestrator.is_running
    data['registry'] = asdict(registry_queue.status())  # Cola de envío hacia M4.
    live = orchestrator.live_summary()
    data['summary'] = None if live is None else summary_payload(live)
    return data
