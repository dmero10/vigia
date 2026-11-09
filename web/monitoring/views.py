"""Capa de presentación. Las vistas sólo inician/detienen y consultan el estado:
la cámara y el análisis corren en el hilo del orquestador, nunca aquí."""

import csv
import logging
import time
from dataclasses import asdict
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from monitoring.orchestrator import COMMANDS, get_orchestrator
from monitoring.registry_client import RegistryError, get_registry_queue

logger = logging.getLogger(__name__)

BRACELET_MODES = {
    'ble': 'Pulsera real',
    'simulated': 'Pulsera simulada',
}
LEVEL_NAMES = {0: 'Sin indicios', 1: 'Señal inicial', 2: 'Atención reducida', 3: 'Riesgo alto'}
DEGRADED_NAMES = {'low_light': 'Poca luz', 'no_face': 'Rostro no detectado'}
VIDEO_FRAME_INTERVAL_S = 1 / 30
REGISTRY_FLUSH_TIMEOUT_S = 2.0
CSV_BOM = '﻿'  # Para que Excel muestre bien las tildes.


def welcome(request):
    """Bienvenida."""
    return render(request, 'monitoring/welcome.html')


def prepare(request):
    """01 · Preparar monitoreo."""
    if get_orchestrator().is_running:
        return redirect('monitoring:session')
    return render(request, 'monitoring/prepare.html', {
        'bracelet_modes': BRACELET_MODES,
        'default_mode': settings.BRACELET_MODE,
    })


@require_POST
def start(request):
    mode = request.POST.get('bracelet_mode', settings.BRACELET_MODE)
    if mode not in BRACELET_MODES:
        return HttpResponseBadRequest('Modo de pulsera inválido.')
    get_orchestrator().start(mode)
    return redirect('monitoring:session')


def session(request):
    """01a cámara, 01b pulsera, 02 calibración, 03 monitoreo, 04 alertas, 06 y 07."""
    orchestrator = get_orchestrator()
    if not orchestrator.is_running and orchestrator.summary() is None:
        return redirect('monitoring:prepare')
    return render(request, 'monitoring/session.html')


@never_cache
def state(request):
    """Estado en JSON; la página de sesión lo consulta varias veces por segundo."""
    data = get_orchestrator().state().as_dict()
    data['running'] = get_orchestrator().is_running
    data['registry'] = asdict(get_registry_queue().status())  # Cola de envío hacia M4.
    return JsonResponse(data)


@never_cache
def video(request):
    """Video en vivo (MJPEG). Los fotogramas sólo pasan por memoria (RNF-05)."""
    orchestrator = get_orchestrator()

    def frames():
        last = None
        while orchestrator.is_running:
            jpeg = orchestrator.latest_jpeg()
            if jpeg is not None and jpeg is not last:
                last = jpeg
                yield (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                       + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n')
            time.sleep(VIDEO_FRAME_INTERVAL_S)

    return StreamingHttpResponse(frames(), content_type='multipart/x-mixed-replace; boundary=frame')


@require_POST
def command(request):
    """Botones de la página de sesión."""
    orchestrator = get_orchestrator()
    action = request.POST.get('action')
    if action in COMMANDS:
        orchestrator.send_command(action)
    elif action == 'reconnect':
        orchestrator.retry_bracelet()
    elif action in ('simulate_disconnect', 'simulate_reconnect'):
        bracelet = orchestrator.bracelet
        if not hasattr(bracelet, 'set_available'):
            return HttpResponseBadRequest('Sólo disponible con la pulsera simulada.')
        bracelet.set_available(action == 'simulate_reconnect')
        if action == 'simulate_reconnect':
            orchestrator.retry_bracelet()
    else:
        return HttpResponseBadRequest('Acción desconocida.')
    return JsonResponse({'ok': True})


@require_POST
def finish(request):
    """«Finalizar sesión»: detiene el hilo (envía nivel 0 a la pulsera)."""
    get_orchestrator().stop()
    return redirect('monitoring:summary')


def summary(request):
    """05 · Resumen de la sesión (RF-12) y 05b · CSV exportado.

    Los datos se piden a M4 por su API. Si M4 no responde, se muestra lo que quedó
    en memoria, para que la demostración nunca se quede sin resumen.
    """
    local = get_orchestrator().summary()
    if local is None:
        return redirect('monitoring:prepare')
    remote = _remote_summary(local)
    if remote is None:
        data = {
            'started_at': local.started_at,
            'duration_s': local.duration_s,
            'alert_count': local.alert_count,
            'max_level': local.max_level,
            'threshold': local.threshold,
        }
        events = [_event_row(event) for event in local.events]
    else:
        info, remote_events = remote
        data = {
            'started_at': datetime.fromisoformat(info['started_at']),
            'duration_s': info['duration_s'],
            'alert_count': info['alert_count'],
            'max_level': info['max_level'],
            'threshold': info['threshold'],
        }
        events = [_remote_event_row(event) for event in remote_events]
    return render(request, 'monitoring/summary.html', {
        'summary': data,
        'duration': _format_duration(data['duration_s']),
        'max_level_name': LEVEL_NAMES[data['max_level']],
        'events': events,
        'csv_name': _csv_name(local),
        'from_registry': remote is not None,
    })


def summary_csv(request):
    """RF-11: eventos con hora, nivel, EAR y MAR en CSV. Sólo métricas, nunca imágenes."""
    local = get_orchestrator().summary()
    if local is None:
        return redirect('monitoring:prepare')
    content = None
    if local.session_id is not None and _registry_ready():
        try:
            content = get_registry_queue().client.events_csv(local.session_id)
        except RegistryError as error:
            logger.warning('No se pudo descargar el CSV de M4: %s', error)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{_csv_name(local)}"'
    if content is not None:
        response.write(content)  # Generado por M4.
        return response
    response.write(CSV_BOM)
    writer = csv.writer(response)
    writer.writerow(['hora', 'nivel', 'estado', 'ear', 'mar'])
    for event in local.events:
        row = _event_row(event)
        writer.writerow([row['time'], row['level'], row['status'],
                         _metric(event.ear), _metric(event.mar)])
    return response


def _registry_ready():
    """Espera un momento a que la cola entregue todo a M4. No afecta a la pulsera:
    la sesión ya terminó."""
    return get_registry_queue().flush(REGISTRY_FLUSH_TIMEOUT_S)


def _remote_summary(local):
    if local.session_id is None or not _registry_ready():
        return None
    client = get_registry_queue().client
    try:
        return client.summary(local.session_id), client.events(local.session_id)
    except RegistryError as error:
        logger.warning('No se pudo leer el resumen de M4: %s', error)
        return None


def _event_row(event):
    if event.degraded_reason:
        status = f'Medición no disponible ({DEGRADED_NAMES.get(event.degraded_reason, event.degraded_reason)})'
    else:
        status = LEVEL_NAMES[event.level]
    return {
        'time': timezone.localtime(event.at).strftime('%Y-%m-%d %H:%M:%S'),
        'level': event.level,
        'status': status,
    }


def _remote_event_row(event):
    at = timezone.localtime(datetime.fromisoformat(event['at']))
    return {'time': at.strftime('%Y-%m-%d %H:%M:%S'), 'level': event['level'],
            'status': event['status']}


def _metric(value):
    return '' if value is None else f'{value:.3f}'


def _csv_name(data):
    return f'vigia_sesion_{timezone.localtime(data.started_at):%Y%m%d_%H%M}.csv'


def _format_duration(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'
