# VIGÍA · Detector de somnolencia

Proyecto de Construcción de Software (UNEMI). Prototipo local que detecta somnolencia
con la webcam y avisa con una pulsera vibratoria por Bluetooth LE.

> **Si trabajas con una IA:** pídele que lea primero [CLAUDE.md](CLAUDE.md). Ahí están
> las reglas de arquitectura, de detección, el protocolo de la pulsera y el estado.
> Este README es la entrada rápida: cómo instalarlo y qué falta.

## Cómo está armado

Son **dos programas** que se comunican por HTTP/JSON:

| Programa | Carpeta | Puerto | Qué hace |
|---|---|---|---|
| Aplicación principal (Django) | `web/` | 8000 | Pantallas, cámara, análisis y pulsera |
| Microservicio M4 (FastAPI + SQLite) | `registry_service/` | 8001 | Guarda sesiones y eventos, resumen y CSV |

Dentro de `web/`: `acquisition` (M1, cámara), `analysis` (M2, EAR/MAR y niveles),
`alerts` (M3, pulsera) y `monitoring` (pantallas y orquestador).

## Instalación (Windows, terminal cmd)

Hace falta **Python 3.11** (Django 5.2 y MediaPipe 0.10.32 están fijados para esa versión).

```
git clone https://github.com/dmero10/vigia.git
cd vigia
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cd web
python manage.py migrate
```

No instalar `opencv-python` aparte: MediaPipe ya trae `opencv-contrib-python`.
El modelo `face_landmarker.task` se descarga solo la primera vez que se usa la cámara.

## Cómo encenderlo (dos ventanas de cmd)

En ambas, primero entrar a la carpeta `vigia` y ejecutar `.venv\Scripts\activate`.

**Ventana 1, M4:**
```
cd registry_service
python -m uvicorn app.main:app --port 8001
```

**Ventana 2, Django:**
```
cd web
python manage.py runserver
```

Abrir http://127.0.0.1:8000. La documentación interactiva de M4 está en
http://127.0.0.1:8001/docs. Para comprobar M4 desde `web`: `python manage.py check_registry`.

En la pantalla «Preparar monitoreo» se elige entre pulsera **simulada** (no necesita
hardware) y **real**.

## Pruebas automáticas

```
cd web
python manage.py test
```
```
cd registry_service
python -m unittest
```

Usan cámara, rostro y pulsera simulados: no necesitan webcam ni hardware.

## Qué está hecho

- **M1, M2 y M3:** captura, EAR/MAR, calibración de 5 s, niveles 0–3, bostezos, medición
  degradada, pulsera simulada y cliente BLE.
- **Pantallas:** recorrido completo del Figma, con video en memoria (nunca se guardan imágenes).
- **M4:** sesiones, eventos con EAR/MAR, resumen y CSV. Django envía los datos desde una
  cola con confirmaciones y reintentos que no frena la alerta a la pulsera.

Medido hasta ahora:

- 30 FPS con análisis durante unos 45 s, con una persona (`check_analysis`).
- Recorrido completo con webcam real (antes de conectar M4).
- M4 de punta a punta con **cámara simulada**: se apagó a mitad de sesión, la alerta siguió
  y los eventos llegaron al volver a encenderlo.

## Qué falta

**Probar M4 con la webcam real.** Con M4 y Django encendidos, hacer una sesión y confirmar
que el resumen dice «Datos guardados en el servicio de registro (M4)». Luego apagar M4
(Ctrl+C) a mitad de otra sesión, provocar una alerta, encenderlo otra vez y finalizar.

**Con la pulsera real.** Ver [firmware/README.md](firmware/README.md); la primera comprobación
es, desde `web`, `python manage.py check_bracelet --real`. Nada de esto se ha medido todavía:

| Qué medir | Requisito |
|---|---|
| El nivel llega en menos de 1 s | RF-07, RNF-01 |
| Patrón de vibración distinto por nivel | RF-08 |
| El motor se apaga tras 3 s sin señal | RF-09 |
| Alcance de al menos 3 m | RNF-04 |
| Dura 30 min con la alimentación actual | RNF-06 |

**Con rostro real.** 3 bostezos en 5 min generan nivel 1 (RF-06); poca luz y salir de cámara
(RF-02); 5 minutos seguidos fluidos (RNF-02).

**Otros pendientes.**

- Guion de la demo.
- Revisar visualmente las pantallas «Mirada recuperada» y «CSV exportado».
- Comprobar cómo abre el CSV en Excel en español: usa comas y podría verse en una columna.
- Opcional, mejoras de precisión: `blendshapes` de MediaPipe (`eyeBlink`, `jawOpen`) y
  comparar la abertura del párpado con el iris.

## Limitaciones conocidas

- La cola hacia M4 está en memoria: si se cierra Django mientras M4 está apagado, se
  pierden los eventos que seguían esperando.
- No hay pantalla de historial de sesiones porque no está en el Figma. El historial se ve
  en `/docs` de M4 o con `check_registry`.

## Reglas al modificarlo

- Las pantallas deben quedar como el Figma; sólo se admiten mejoras pequeñas.
- La captura y el análisis corren en un hilo, nunca dentro de una vista HTTP.
- Django nunca lee la base de M4: sólo usa su API.
- Nunca guardar imágenes ni video del rostro, sólo métricas (RNF-05).
- Niveles sólo 0–3: el nivel 4 fue eliminado.
- Reportar únicamente resultados medidos (latencia, FPS, alcance).
- Código con identificadores en inglés; interfaz y comentarios en español.

La fuente oficial de requisitos es el documento *Componente Práctico – Detector de
somnolencia*, que no está en este repositorio: pedirlo al grupo.
