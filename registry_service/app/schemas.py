"""Contrato de la API (JSON). Mientras este contrato se mantenga, M4 puede
actualizarse sin reemplazar la aplicación principal.

RNF-05: los modelos de entrada rechazan cualquier campo que no esté aquí
(`extra='forbid'`), así que no hay forma de enviar imágenes o video del rostro.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

DegradedReason = Literal['low_light', 'no_face']
BraceletMode = Literal['simulated', 'ble']
Identifier = Annotated[str, Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')]


class _Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


# ---------- Entrada ----------

class SessionCreate(_Input):
    """La crea la aplicación principal al pulsar «Iniciar monitoreo»."""

    id: Identifier  # Lo genera el cliente: así los reintentos no duplican sesiones.
    started_at: AwareDatetime
    bracelet_mode: BraceletMode | None = None


class SessionFinish(_Input):
    ended_at: AwareDatetime
    threshold: float | None = Field(default=None, ge=0, le=1)  # Umbral EAR calibrado.


class EventIn(_Input):
    """RF-11: un cambio de nivel o de estado de la medición, con su hora y métricas."""

    id: Identifier  # Único: si llega dos veces por un reintento, se guarda una sola.
    at: AwareDatetime
    level: int = Field(ge=0, le=3)  # 0 = normal; 1, 2, 3 = alerta.
    degraded_reason: DegradedReason | None = None
    ear: float | None = Field(default=None, ge=0)
    mar: float | None = Field(default=None, ge=0)
    closed_ms: int = Field(default=0, ge=0)
    yawn_count: int = Field(default=0, ge=0)


class EventBatch(_Input):
    events: list[EventIn] = Field(min_length=1, max_length=500)


# ---------- Salida ----------

class BatchResult(BaseModel):
    """Confirmación para la cola de envío de la aplicación principal."""

    accepted: int
    duplicates: int


class SessionOut(BaseModel):
    id: str
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    status: Literal['open', 'finished']
    bracelet_mode: str | None
    threshold: float | None
    event_count: int


class EventOut(BaseModel):
    id: str
    at: AwareDatetime
    level: int
    status: str  # Texto para mostrar, p. ej. «Señal inicial».
    degraded_reason: str | None
    ear: float | None
    mar: float | None
    closed_ms: int
    yawn_count: int


class SummaryOut(BaseModel):
    """RF-12: número de alertas, nivel máximo y duración."""

    session_id: str
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    finished: bool
    duration_s: float
    alert_count: int
    max_level: int
    max_level_name: str
    threshold: float | None
    event_count: int


class Health(BaseModel):
    status: Literal['ok']
    service: str
    version: str
    database: Literal['ok']
