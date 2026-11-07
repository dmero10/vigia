# VIGÍA – Contexto del proyecto

Proyecto universitario (Construcción de Software, UNEMI, grupo de 5). Prototipo
local que detecta somnolencia con la webcam y avisa con una pulsera vibratoria
por Bluetooth LE. **La presentación es la semana del 10 de noviembre de 2026 y
debe mostrarse todo funcionando.** Fuente oficial de requisitos: documento
*Componente Práctico – Detector de somnolencia* (las tareas anteriores tienen
datos desactualizados, como los niveles 0–4).

## Forma de trabajar con el usuario
- Responder en español, claro y paso a paso; explicar qué hace cada comando.
- Windows + VS Code con terminal **cmd** (no PowerShell). Dar comandos de a uno.
- El usuario quiere la estructura clásica de Django con nombres en inglés.
  Identificadores en inglés; textos de la interfaz y comentarios en español.
- No inventar resultados de pruebas (latencia, FPS, alcance): sólo reportar lo medido.

## Entorno
- Python **3.11** (también hay 3.13 instalado: usar siempre `py -3.11`).
- Entorno virtual en la raíz: `.venv\Scripts\activate`.
- Django **5.2** (Django 6 no funciona con Python 3.11).
- MediaPipe **0.10.32**: trae `opencv-contrib-python`; **no** instalar `opencv-python` aparte.
- Git: repo `github.com/dmero10/vigia`, rama `main`.

## Estructura
```
vigia/
├── .venv/               (no se sube)
├── requirements.txt
├── web/                 proyecto Django (aplicación principal, monolito modular)
│   ├── manage.py
│   ├── vigia/           settings.py, urls.py (LANGUAGE_CODE='es', TIME_ZONE='America/Guayaquil')
│   ├── monitoring/      pantallas (views, urls, templates, static) + orquestador en segundo plano
│   ├── acquisition/     M1: cámara (OpenCV) y validación del video
│   ├── analysis/        M2: puntos faciales, EAR/MAR, calibración, niveles
│   └── alerts/          M3: niveles a enviar y cliente BLE (Bleak) de la pulsera
├── registry_service/    M4: microservicio FastAPI + SQLite (proceso aparte)
├── firmware/            sketch Arduino del M5StickS3 (pendiente de recibir)
└── docs/
```

Capas (Figura 2 del documento):
- Presentación: `monitoring/views.py` + templates (Django MVT).
- Lógica: `services.py` / módulos de cada app (M1, M2, M3).
- Infraestructura: `acquisition/camera.py` (OpenCV), `alerts/ble_client.py` (Bleak),
  `monitoring/registry_client.py` (HTTP/JSON a M4 con cola y reintentos).

## Reglas de arquitectura
- La captura y el análisis corren en un hilo en segundo plano, **nunca** dentro de
  una vista HTTP. Las vistas sólo inician/detienen y consultan el estado.
- Iniciar el hilo al pulsar «Iniciar monitoreo», no al arrancar Django: el
  autorecargador de `runserver` crea dos procesos y abriría la cámara dos veces.
- M4 tiene su propia base SQLite; Django nunca la lee directamente, sólo usa su API.
  `db.sqlite3` de Django es sólo para configuración.
- El envío de eventos a M4 no debe bloquear la alerta a la pulsera.
- RNF-05: nunca guardar imágenes ni video del rostro; sólo métricas.

## Reglas de detección (M2)
- Niveles: **0 = normal; 1, 2, 3 = alerta.** El nivel 4 fue eliminado.
- Cierre ocular continuo: ≥1.5 s → 1, ≥3 s → 2, ≥5 s → 3 (valores iniciales, ajustables).
  Al reabrir los ojos vuelve a 0. Ojos abiertos ≥0.2 s = fin del cierre (evita ruido).
- Calibración RF-04: 5 s con ojos abiertos, mínimo 30 muestras;
  umbral = mediana(EAR) × 0.75, limitado entre 0.12 y 0.30.
- Bostezos RF-06: MAR > 0.6 durante ≥1 s cuenta un bostezo; 3 bostezos en 5 min
  generan nivel 1 durante 3 s y el contador se reinicia.
- Ajuste propio (no está en el documento oficial; medido con una sola persona): con
  MAR > 0.5 y hasta 3 s después, el umbral de cierre se multiplica por 0.6, porque
  bostezar con los ojos abiertos baja el EAR y generaba alertas falsas.
- RF-02: con luz media < 40 (0–255) o sin rostro > 0.5 s → "medición degradada",
  se reinicia el cierre en curso y se envía nivel 0 (no mantener alertas anteriores).
- MediaPipe 0.10.32 **ya no tiene `mp.solutions`**: usar la Tasks API
  (`mediapipe.tasks.python.vision.FaceLandmarker`, `RunningMode.VIDEO`,
  `detect_for_video` con milisegundos estrictamente crecientes). El modelo
  `face_landmarker.task` se descarga de
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
  y no se sube a Git.
- Convertir los puntos normalizados a píxeles (x·ancho, y·alto) antes de calcular EAR/MAR.
- Índices (malla de 478 puntos), orden p1..p6:
  ojo derecho (33, 160, 158, 133, 153, 144), ojo izquierdo (362, 385, 387, 263, 373, 380).
  EAR = (|p2−p6| + |p3−p5|) / (2·|p1−p4|), promedio de ambos ojos.
  Boca: esquinas (61, 291); verticales (81,178), (13,14), (311,402); MAR = promedio vertical / ancho.
- No se entrena un modelo propio; MediaPipe ya está entrenado.

## Pulsera (verificado en `firmware/pulsera_sticks3_final/pulsera_sticks3_final.ino`)
M5Stack StickS3 (ESP32-S3) + Unit Vibrator U059 por Grove en G9 (no G1/G2),
salida 5 V del Grove habilitada, firmware en Arduino IDE, anuncio BLE
`PulseraSomnolencia`. Servicio `12345678-1234-1234-1234-1234567890ab`,
característica `abcd1234-5678-90ab-cdef-1234567890ab` (escritura con respuesta).
Mensaje = nivel como un dígito ASCII (`"2"`); el latido reenvía el nivel cada 1 s.
El firmware apaga el motor si pasan 3 s sin mensajes (RF-09) y al desconectarse.
Acepta 0–4 (versión vieja), Python sólo envía 0–3; un carácter no numérico lo
interpretaría como 4. Necesita USB o powerbank. Siempre debe existir un modo con
pulsera simulada para la demo.

## Requisitos clave
RF-01 video ≥25 FPS · RF-02 medición degradada · RF-03 EAR/MAR por fotograma ·
RF-04 calibración 5 s · RF-05 niveles 0–3 · RF-06 bostezos · RF-07 conectar/reintentar
BLE y enviar nivel <1 s · RF-08 patrón por nivel · RF-09 apagado tras 3 s sin señal ·
RF-10 iniciar desde la interfaz · RF-11 eventos con hora y nivel + CSV ·
RF-12 resumen (n.º de alertas, nivel máximo, duración).
RNF-01 latencia ≤1 s · RNF-02 5 min fluido · RNF-03 umbral por usuario ·
RNF-04 BLE ≥3 m · RNF-05 sin imágenes · RNF-06 pulsera 30 min con la alimentación actual.

## Pantallas (Figma, a replicar en templates de `monitoring`)
Bienvenida/presentación → Preparar monitoreo → Conectar cámara → Conectar pulsera →
Calibración (5 s) → Monitoreo activo (nivel, EAR, MAR, bostezos, estado pulsera) →
Alerta nivel 1/2/3 → Ojos abiertos (vuelve a 0) → Medición no disponible →
Pulsera desconectada → Resumen de sesión → CSV exportado.
Copia de trabajo del Figma: https://www.figma.com/design/mlepvoPoUQvulXCQ78FwE8/
(el original no se modifica).

## Estado
- [x] Estructura Django creada (4 apps registradas) y subida a GitHub.
- [x] acquisition: camera.py, validation.py, tests, comando `check_camera`.
  En Windows usar el backend MSMF: con DirectShow la webcam sólo dio 10-20 FPS.
- [x] analysis: landmarks.py, metrics.py, calibration.py, classifier.py + tests, comando
  `check_analysis`. Probado con una persona: 30 FPS con análisis durante unos 45 s.
  Sin probar con rostro real: 3 bostezos, poca luz y falta de rostro.
- [ ] alerts: pulsera simulada, AlertSender (hilo propio, latido 1 s, reintentos),
  ble_client.py (Bleak 3.0.2) y comando `check_bracelet [--real]` listos.
  Falta probar con la pulsera real: latencia (RF-07), patrones (RF-08), RF-09, alcance.
- [ ] monitoring: orquestador en hilo, vistas, templates, video en vivo.
- [ ] registry_service: FastAPI + SQLite, sesiones, eventos, resumen, CSV.
- [ ] Pruebas de RNF y guion de la demo.
