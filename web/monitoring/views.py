"""Capa de presentación. Las vistas sólo inician/detienen y consultan el estado:
la cámara y el análisis corren en el hilo del orquestador, nunca aquí.

Los datos en tiempo real llegan a la página por WebSocket (consumers.py); la
vista `state` devuelve lo mismo en JSON y queda como respaldo.
"""

import asyncio
import csv
import logging
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from alerts.bracelet import DEFAULT_STRENGTH, MAX_STRENGTH, MIN_STRENGTH
from monitoring.orchestrator import COMMANDS, get_orchestrator
from monitoring.presenters import (
    LEVEL_NAMES,
    STRENGTH_NAMES,
    event_row,
    format_duration,
    live_payload,
    rnf02_text,
    summary_payload,
)
from monitoring.registry_client import RegistryError, get_registry_queue

logger = logging.getLogger(__name__)

BRACELET_MODES = {
    'ble': 'Pulsera real',
    'simulated': 'Pulsera simulada',
}
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
    return render(request, 'monitoring/session.html', {
        'strength_min': MIN_STRENGTH,
        'strength_max': MAX_STRENGTH,
        'strength_default': DEFAULT_STRENGTH,
        'strength_names': STRENGTH_NAMES,
    })


@never_cache
def state(request):
    """Estado en JSON: lo mismo que envía el WebSocket (respaldo si no hay WebSocket)."""
    return JsonResponse(live_payload(get_orchestrator(), get_registry_queue()))


@never_cache
def video(request):
    """Video en vivo (MJPEG). Los fotogramas sólo pasan por memoria (RNF-05).

    Es un generador asíncrono: con el servidor ASGI (Daphne) no ocupa el hilo
    que atiende al resto de las vistas.
    """
    orchestrator = get_orchestrator()

    async def frames():
        last = None
        while orchestrator.is_running:
            jpeg = orchestrator.latest_jpeg()
            if jpeg is not None and jpeg is not last:
                last = jpeg
                yield (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                       + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n')
            await asyncio.sleep(VIDEO_FRAME_INTERVAL_S)

    return StreamingHttpResponse(frames(), content_type='multipart/x-mixed-replace; boundary=frame')


@require_POST
def command(request):
    """Botones y deslizador de la página de sesión."""
    orchestrator = get_orchestrator()
    action = request.POST.get('action')
    if action in COMMANDS:
        orchestrator.send_command(action)
    elif action == 'strength':
        try:
            orchestrator.set_strength(int(request.POST.get('value', '')))
        except ValueError:
            return HttpResponseBadRequest('Fuerza inválida: debe estar entre 1 y 5.')
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
    """«Finalizar»: detiene el hilo (envía nivel 0 a la pulsera y apaga la cámara)."""
    get_orchestrator().stop()
    return redirect('monitoring:summary')


def summary(request):
    """05 · Resumen de la sesión (RF-12) y 05b · CSV exportado.

    Dos momentos:
    - En vivo: tras el primer «Finalizar sesión» el monitoreo sigue (cámara,
      análisis y pulsera) y el resumen se actualiza por WebSocket. El botón
      «Finalizar» lo detiene todo.
    - Final: los datos se piden a M4 por su API. Si M4 no responde, se muestra
      lo que quedó en memoria, para que la demostración nunca se quede sin resumen.
    """
    orchestrator = get_orchestrator()
    live = orchestrator.live_summary()
    if live is not None:
        return render(request, 'monitoring/summary.html', {
            'live': True,
            'summary': summary_payload(live),
            'started_at': live.started_at,
            'csv_name': _csv_name(live),
        })
    if orchestrator.is_running:
        return redirect('monitoring:session')  # Aún no empezó el monitoreo.

    local = orchestrator.summary()
    if local is None:
        return redirect('monitoring:prepare')
    data = summary_payload(local)
    started_at = local.started_at
    remote = _remote_summary(local)
    if remote is not None:
        info, remote_events = remote
        started_at = datetime.fromisoformat(info['started_at'])
        data.update({
            'duration': format_duration(info['duration_s']),
            'alert_count': info['alert_count'],
            'max_level': info['max_level'],
            'max_level_name': LEVEL_NAMES[info['max_level']],
            'threshold': info['threshold'],
            'events': [_remote_event_row(event) for event in remote_events],
        })
        if info.get('fps_min') is not None:
            data.update({
                'monitoring_s': info['monitoring_s'],
                'fps_avg': info['fps_avg'],
                'fps_min': info['fps_min'],
                'rnf02': rnf02_text(info['monitoring_s'] or 0, info['fps_min']),
            })
    return render(request, 'monitoring/summary.html', {
        'live': False,
        'summary': data,
        'started_at': started_at,
        'csv_name': _csv_name(local),
        'from_registry': remote is not None,
    })


def summary_csv(request):
    """RF-11: eventos con hora, nivel, EAR y MAR en CSV. Sólo métricas, nunca imágenes."""
    orchestrator = get_orchestrator()
    if orchestrator.is_running:
        return redirect('monitoring:summary')  # Se exporta al finalizar del todo.
    local = orchestrator.summary()
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
        row = event_row(event)
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


def _remote_event_row(event):
    at = timezone.localtime(datetime.fromisoformat(event['at']))
    return {'time': at.strftime('%Y-%m-%d %H:%M:%S'), 'level': event['level'],
            'status': event['status']}


def _metric(value):
    return '' if value is None else f'{value:.3f}'


def _csv_name(data):
    return f'vigia_sesion_{timezone.localtime(data.started_at):%Y%m%d_%H%M}.csv'
