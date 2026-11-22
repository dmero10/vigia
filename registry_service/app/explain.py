"""Explica en palabras el registro de una sesión, para leerlo sin conocer el sistema.

Este archivo existe dos veces y debe ser IDÉNTICO en las dos:
- registry_service/app/explain.py (M4: CSV y API)
- web/monitoring/explain.py (Django: resumen en vivo y respaldo sin M4)
Una prueba de Django comprueba que no se separen.

Cada evento es un dict con: at (datetime), level (0-3), degraded_reason
('low_light', 'no_face' o None), closed_ms y yawn_count. Un evento se guarda
cada vez que cambia el nivel o el estado de la medición.

También arma el CSV (RF-11), para que M4 y el respaldo de Django den el mismo archivo.
"""

import csv
import io

LEVEL_NAMES = {0: 'Sin indicios', 1: 'Señal inicial', 2: 'Atención reducida', 3: 'Riesgo alto'}
DEGRADED_DETAILS = {'low_light': 'Hay poca luz', 'no_face': 'No se ve el rostro'}
CAUSE_EYES = 'Ojos cerrados'
CAUSE_YAWNS = 'Bostezos frecuentes'
STILL_ACTIVE = 'Seguía activa al finalizar'
BRACELET_NAMES = {'simulated': 'Simulada', 'ble': 'Real (Bluetooth)'}
RNF02_MIN_MONITORING_S = 300.0  # RNF-02: 5 minutos de monitoreo...
RNF02_MIN_FPS = 25.0  # ...sin bajar de 25 FPS.
# Punto y coma: Excel en español separa las columnas con «;» (con «,» todo queda en una).
CSV_DELIMITER = ';'
CSV_BOM = '\ufeff'  # Para que Excel muestre bien las tildes.


def format_seconds(seconds):
    """4.2 s · 15 s · 1 min 05 s"""
    if seconds < 10:
        return f'{seconds:.1f} s'
    if seconds < 60:
        return f'{seconds:.0f} s'
    minutes, rest = divmod(int(round(seconds)), 60)
    return f'{minutes} min {rest:02d} s'


def _why(closed_ms):
    if closed_ms:
        return f'Ojos cerrados {format_seconds(closed_ms / 1000)} seguidos'
    return 'Bostezos frecuentes: 3 en menos de 5 minutos'


def explain(events, ended_at=None):
    """Devuelve (registro, alertas).

    - registro: una fila por evento, con `event` (qué pasó) y `detail` (por qué).
    - alertas: una fila por alerta. Una alerta empieza cuando el nivel sube de 0
      y termina cuando vuelve a 0 (o cuando la medición deja de estar disponible).
    """
    log = []
    alerts = []
    previous_level = 0
    previous_degraded = None
    current = None  # Alerta en curso.

    for event in events:
        level = event['level']
        reason = event.get('degraded_reason')
        closed_ms = event.get('closed_ms') or 0
        at = event['at']
        alert_number = current['number'] if current is not None else None

        if reason:
            title = 'Medición no disponible'
            detail = DEGRADED_DETAILS.get(reason, reason)
            if current is not None:
                seconds = _close(current, at, 'Se cortó: medición no disponible')
                detail += f'. La alerta se corta tras {format_seconds(seconds)}'
                current = None
        elif level > 0 and (previous_level == 0 or current is None):
            title = f'Empieza una alerta (nivel {level})'
            detail = _why(closed_ms)
            current = {
                'number': len(alerts) + 1,
                'start': at,
                'max_level': level,
                'cause': CAUSE_EYES if closed_ms else CAUSE_YAWNS,
                'duration_s': None,
                'ended_by': STILL_ACTIVE,
            }
            alerts.append(current)
            alert_number = current['number']
        elif level > previous_level:
            title = f'Sube a nivel {level}'
            detail = _why(closed_ms)
            current['max_level'] = max(current['max_level'], level)
        elif level > 0:
            title = f'Baja a nivel {level}'
            detail = 'La alerta sigue activa'
        elif current is not None:
            ended_by = 'Abrió los ojos' if current['cause'] == CAUSE_EYES else 'Pasó el aviso por bostezos'
            seconds = _close(current, at, ended_by)
            title = 'Termina la alerta'
            detail = f'{ended_by}. La alerta duró {format_seconds(seconds)}'
            current = None
        elif previous_degraded:
            title = 'Medición recuperada'
            detail = 'Se vuelve a medir con normalidad'
        else:
            title = 'Nivel normal'
            detail = ''

        log.append({
            'alert': alert_number,  # Alerta a la que pertenece esta fila (o None).
            'at': at,
            'level': level,
            'degraded': bool(reason),
            'event': title,
            'detail': detail,
        })
        previous_level, previous_degraded = level, reason

    if current is not None and ended_at is not None:
        current['duration_s'] = max(0.0, (ended_at - current['start']).total_seconds())
    for alert in alerts:
        alert['max_level_name'] = LEVEL_NAMES[alert['max_level']]
    return log, alerts


def _close(alert, at, ended_by):
    seconds = max(0.0, (at - alert['start']).total_seconds())
    alert['duration_s'] = seconds
    alert['ended_by'] = ended_by
    return seconds


def format_clock(seconds):
    """00:02:40"""
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'


def rnf02_result(monitoring_s, fps_min):
    """Resultado de la prueba de 5 minutos fluidos (RNF-02), en palabras."""
    if fps_min is None or monitoring_s is None:
        return 'Sin datos de FPS'
    if fps_min < RNF02_MIN_FPS:
        return f'No cumplida: el FPS bajó a {fps_min:.0f} (mínimo {RNF02_MIN_FPS:.0f})'
    if monitoring_s < RNF02_MIN_MONITORING_S:
        return (f'No cumplida: {format_clock(monitoring_s)} de monitoreo '
                f'(se piden {RNF02_MIN_MONITORING_S / 60:.0f} min)')
    return (f'Cumplida: {format_clock(monitoring_s)} de monitoreo '
            f'sin bajar de {RNF02_MIN_FPS:.0f} FPS')


def build_csv(session, events):
    """CSV de la sesión en cuatro bloques: resumen, alertas, registro detallado y
    cómo leerlo. Sólo medidas, nunca imágenes (RNF-05).

    `session`: dict con started_at, ended_at (None si sigue abierta), duration_s,
    alert_count, max_level, threshold, monitoring_s, fps_avg, fps_min y,
    opcional, bracelet_mode. `events`: dicts como los de `explain`, con ear y mar.
    Las horas deben venir ya en la zona horaria local.
    """
    log, alerts = explain(events, session['ended_at'])
    buffer = io.StringIO()
    buffer.write(CSV_BOM)
    writer = csv.writer(buffer, delimiter=CSV_DELIMITER, lineterminator='\r\n')
    started, ended = session['started_at'], session['ended_at']
    max_level = session['max_level']

    writer.writerow(['VIGÍA · Registro de la sesión de monitoreo'])
    writer.writerow([])
    writer.writerow(['RESUMEN'])
    writer.writerow(['Fecha', started.strftime('%Y-%m-%d')])
    writer.writerow(['Hora de inicio', started.strftime('%H:%M:%S')])
    writer.writerow(['Hora de fin', ended.strftime('%H:%M:%S') if ended else 'Sesión abierta'])
    writer.writerow(['Duración total', format_clock(session['duration_s'])])
    if session.get('bracelet_mode'):
        mode = session['bracelet_mode']
        writer.writerow(['Pulsera', BRACELET_NAMES.get(mode, mode)])
    writer.writerow(['Número de alertas', session['alert_count']])
    writer.writerow(['Nivel máximo', f'{max_level} · {LEVEL_NAMES[max_level]}'])
    threshold = session.get('threshold')
    writer.writerow(['Umbral de ojos cerrados (EAR)',
                     'Sin calibrar' if threshold is None else f'{threshold:.3f}'])
    fps_avg, fps_min = session.get('fps_avg'), session.get('fps_min')
    if fps_avg is not None and fps_min is not None:
        writer.writerow(['FPS promedio', f'{fps_avg:.1f}'])
        writer.writerow(['FPS mínimo', f'{fps_min:.1f}'])
    writer.writerow(['Prueba de 5 min fluidos (RNF-02)',
                     rnf02_result(session.get('monitoring_s'), fps_min)])
    writer.writerow([])

    writer.writerow(['ALERTAS DE LA SESIÓN'])
    if alerts:
        writer.writerow(['Alerta N.º', 'Hora de inicio', 'Duración', 'Nivel máximo',
                         'Motivo', 'Cómo terminó'])
        for alert in alerts:
            duration = alert['duration_s']
            still_open = ended is None and alert['ended_by'] == STILL_ACTIVE
            writer.writerow([
                alert['number'],
                alert['start'].strftime('%H:%M:%S'),
                'Sin dato' if duration is None else format_seconds(duration),
                f"{alert['max_level']} · {alert['max_level_name']}",
                alert['cause'],
                'Sigue activa' if still_open else alert['ended_by'],
            ])
    else:
        writer.writerow(['No hubo alertas en esta sesión'])
    writer.writerow([])

    writer.writerow(['REGISTRO DETALLADO (una fila por cada cambio)'])
    writer.writerow(['Hora', 'Nivel', 'Estado', 'Alerta N.º', 'Qué pasó', 'Detalle',
                     'EAR (ojos)', 'MAR (boca)'])
    for row, event in zip(log, events):
        writer.writerow([
            row['at'].strftime('%H:%M:%S'),
            row['level'],
            'Medición no disponible' if row['degraded'] else LEVEL_NAMES[row['level']],
            row['alert'] or '',
            row['event'],
            row['detail'],
            _metric(event.get('ear')),
            _metric(event.get('mar')),
        ])
    if not log:
        writer.writerow(['Sin cambios registrados'])
    writer.writerow([])

    writer.writerow(['CÓMO LEERLO'])
    writer.writerow(['Nivel', '0 = sin indicios · 1 = señal inicial · '
                              '2 = atención reducida · 3 = riesgo alto'])
    writer.writerow(['Alerta', 'Empieza cuando el nivel sube de 0 y termina cuando vuelve a 0'])
    writer.writerow(['Nivel por ojos', 'Ojos cerrados seguidos: 1.5 s = nivel 1 · '
                                       '3 s = nivel 2 · 5 s = nivel 3'])
    writer.writerow(['Nivel por bostezos', '3 bostezos en 5 minutos = nivel 1 durante 3 s'])
    writer.writerow(['EAR (ojos)', 'Abertura de los ojos: cuanto más bajo, más cerrados. '
                                   'Por debajo del umbral cuentan como cerrados'])
    writer.writerow(['MAR (boca)', 'Abertura de la boca: más de 0.600 durante 1 s es un bostezo'])
    writer.writerow(['Medición no disponible', 'Poca luz o rostro fuera de cámara: '
                                               'se pausa la medición y no se envían alertas'])
    writer.writerow(['Privacidad', 'Sólo se guardan medidas, nunca imágenes ni video del rostro'])
    return buffer.getvalue()


def _metric(value):
    return '' if value is None else f'{value:.3f}'
