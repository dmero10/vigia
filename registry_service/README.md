# M4 · Microservicio de registro de sesiones (FastAPI + SQLite)

Proceso independiente de la aplicación principal (Django). Guarda las sesiones de
monitoreo y sus eventos (hora, nivel 0–3, EAR, MAR), calcula el resumen (RF-12) y
exporta el CSV (RF-11). **No recibe ni guarda imágenes del rostro** (RNF-05): la API
rechaza con error 422 cualquier campo que no esté en el contrato.

## Capas

| Capa | Archivo | Qué hace |
|---|---|---|
| API de entrada | `app/main.py` | Rutas HTTP/JSON (FastAPI). No tiene lógica. |
| Contrato | `app/schemas.py` | Qué datos entran y salen, y sus validaciones (nivel 0–3, fechas con zona horaria…). |
| Lógica de sesiones | `app/services.py` | Resumen (n.º de alertas, nivel máximo, duración) y CSV. |
| Acceso a datos | `app/repository.py` | Única parte que conoce SQLite. |

La base de datos es propia de M4 (`data/registry.sqlite3`, no se sube a Git).
Django nunca la lee: sólo usa la API.

## Cómo encenderlo (terminal cmd aparte)

```
cd rutadearpetaigia
.venv\Scripts\activate
cd registry_service
python -m uvicorn app.main:app --port 8001
```

- Documentación interactiva para probar la API: http://127.0.0.1:8001/docs
- Comprobar desde Django (en otra terminal, dentro de `web`): `python manage.py check_registry`

## API (versión 1)

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/health` | ¿Está vivo el servicio y su base de datos? |
| POST | `/api/v1/sessions` | Crear una sesión (201; si ya existía, 200 sin duplicarla). |
| GET | `/api/v1/sessions` | Historial de sesiones. |
| GET | `/api/v1/sessions/{id}` | Datos de una sesión. |
| POST | `/api/v1/sessions/{id}/finish` | Hora de fin y umbral calibrado. |
| POST | `/api/v1/sessions/{id}/events` | Recibir uno o varios eventos; responde la confirmación `{accepted, duplicates}`. |
| GET | `/api/v1/sessions/{id}/events` | Eventos de la sesión. |
| GET | `/api/v1/sessions/{id}/summary` | Resumen: n.º de alertas, nivel máximo, duración. |
| GET | `/api/v1/sessions/{id}/events.csv` | Exportar CSV (`hora,nivel,estado,ear,mar`). |

Una **alerta** se cuenta cada vez que el nivel pasa de 0 a 1, 2 o 3.

## Comunicación con la aplicación principal

Django (`web/monitoring/registry_client.py`) envía los datos desde una **cola en su
propio hilo, con confirmaciones y reintentos**:

- Encolar no espera a la red: el registro nunca retrasa la alerta a la pulsera.
- Si M4 está apagado, los datos esperan en la cola y se envían en orden cuando vuelve.
- Los identificadores de sesión y de evento los genera Django, así que un reenvío
  nunca se guarda dos veces.
- Si M4 no responde al terminar, la pantalla de resumen muestra los datos de memoria
  y lo indica.

Mientras se mantenga este contrato, M4 puede actualizarse sin tocar la aplicación principal.

## Pruebas

```
cd registry_service
python -m unittest -v
```

Variables de entorno opcionales: `VIGIA_REGISTRY_DB` (ruta de la base) y
`VIGIA_TIME_ZONE` (por defecto `America/Guayaquil`). En Django, `VIGIA_REGISTRY_URL`
(por defecto `http://127.0.0.1:8001`).
