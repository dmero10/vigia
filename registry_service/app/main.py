"""Capa de API de entrada (FastAPI). Sólo recibe y responde JSON/CSV; la lógica
está en services.py y el acceso a SQLite en repository.py.

Arranque (desde la carpeta registry_service):
    python -m uvicorn app.main:app --port 8001
Documentación interactiva: http://127.0.0.1:8001/docs
"""

from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app import config
from app.repository import Repository
from app.schemas import (
    BatchResult,
    EventBatch,
    EventOut,
    Health,
    SessionCreate,
    SessionFinish,
    SessionOut,
    SummaryOut,
)
from app.services import RegistryService, SessionNotFound

DESCRIPTION = """
Microservicio **M4** de VIGÍA. Guarda las sesiones de monitoreo y sus eventos
(hora, nivel 0–3 y métricas EAR/MAR), genera el resumen (RF-12) y exporta el
CSV (RF-11). **No recibe ni guarda imágenes del rostro** (RNF-05): cualquier
campo fuera del contrato se rechaza con el error 422.

La aplicación principal (Django) envía los datos desde una cola en segundo plano
con confirmaciones y reintentos; los identificadores de sesión y de evento los
genera el cliente, por eso reenviar un dato nunca lo duplica.
"""


def create_app(db_path=None):
    service = RegistryService(Repository(db_path or config.DB_PATH), config.TIME_ZONE)

    @asynccontextmanager
    async def lifespan(app):
        service.repository.init_schema()
        yield

    app = FastAPI(
        title='VIGÍA · Registro de sesiones (M4)',
        version=config.API_VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.service = service

    @app.exception_handler(SessionNotFound)
    async def session_not_found(request, exc):
        return JSONResponse(status_code=404, content={'detail': 'Sesión no encontrada.'})

    @app.get('/health', response_model=Health, tags=['estado'])
    def health(service: RegistryService = Depends(get_service)):
        """Comprueba que el servicio y su base de datos responden."""
        service.repository.ping()
        return Health(status='ok', service='vigia-registry', version=config.API_VERSION,
                      database='ok')

    app.include_router(router)
    return app


def get_service(request: Request) -> RegistryService:
    return request.app.state.service


router = APIRouter(prefix='/api/v1')


@router.post('/sessions', response_model=SessionOut, status_code=status.HTTP_201_CREATED,
             tags=['sesiones'], responses={200: {'description': 'La sesión ya existía'}})
def create_session(data: SessionCreate, response: Response,
                   service: RegistryService = Depends(get_service)):
    """Registra una sesión nueva. Si se reenvía la misma, responde 200 sin duplicarla."""
    session, created = service.create_session(data)
    if not created:
        response.status_code = status.HTTP_200_OK
    return session


@router.get('/sessions', response_model=list[SessionOut], tags=['sesiones'])
def list_sessions(limit: int = Query(50, ge=1, le=500),
                  service: RegistryService = Depends(get_service)):
    """Historial de sesiones, de la más reciente a la más antigua."""
    return service.list_sessions(limit)


@router.get('/sessions/{session_id}', response_model=SessionOut, tags=['sesiones'])
def get_session(session_id: str, service: RegistryService = Depends(get_service)):
    return service.get_session(session_id)


@router.post('/sessions/{session_id}/finish', response_model=SessionOut, tags=['sesiones'])
def finish_session(session_id: str, data: SessionFinish,
                   service: RegistryService = Depends(get_service)):
    """Marca la hora de fin y guarda el umbral calibrado."""
    return service.finish_session(session_id, data)


@router.post('/sessions/{session_id}/events', response_model=BatchResult, tags=['eventos'])
def add_events(session_id: str, batch: EventBatch,
               service: RegistryService = Depends(get_service)):
    """Recibe uno o varios eventos. La respuesta es la confirmación para la cola."""
    return service.add_events(session_id, batch)


@router.get('/sessions/{session_id}/events', response_model=list[EventOut], tags=['eventos'])
def list_events(session_id: str, service: RegistryService = Depends(get_service)):
    return service.list_events(session_id)


@router.get('/sessions/{session_id}/summary', response_model=SummaryOut, tags=['resumen'])
def summary(session_id: str, service: RegistryService = Depends(get_service)):
    """RF-12: número de alertas, nivel máximo y duración."""
    return service.summary(session_id)


@router.get('/sessions/{session_id}/events.csv', tags=['resumen'],
            response_class=Response,
            responses={200: {'content': {'text/csv': {}}, 'description': 'Archivo CSV'}})
def export_csv(session_id: str, service: RegistryService = Depends(get_service)):
    """RF-11: todos los eventos con hora, nivel, estado, EAR y MAR."""
    content = service.export_csv(session_id)
    filename = service.csv_filename(session_id)
    return Response(
        content=content.encode('utf-8'),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


app = create_app()
