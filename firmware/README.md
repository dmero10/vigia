# Firmware de la pulsera M5StickS3

`pulsera_sticks3_final/pulsera_sticks3_final.ino` se abre y se carga con Arduino IDE
(la carpeta debe llamarse igual que el `.ino`).

Datos que usa Python (`web/alerts/ble_client.py`):

- Nombre BLE: `PulseraSomnolencia`
- Servicio: `12345678-1234-1234-1234-1234567890ab`
- Característica (escritura con respuesta): `abcd1234-5678-90ab-cdef-1234567890ab`
- Mensaje: dos dígitos de texto, **nivel** (`0`–`3`) y **fuerza** (`1`–`5`).
  Ejemplo: `"23"` = nivel 2 con fuerza 3. Cualquier mensaje reinicia el contador de
  3 s (RF-09), así que el latido de Python reenvía el mensaje cada 1 s.
- Motor U059 en el Grove G9 con salida de 5 V habilitada.

## Versión actual (noviembre 2026)

- **Fuerza elegida por el usuario:** el segundo dígito escala la intensidad de todos los
  niveles (`PORCENTAJE_FUERZA`: 50 %, 62 %, 75 %, 88 % y 100 %). Si sólo llega el nivel
  (`"2"`), usa la fuerza 3.
- **Arranque suave:** la potencia sube de a poco (`RAMPA_PASO` cada 10 ms) en lugar de
  saltar de 0 al máximo.
- **Nivel 3 más suave:** 350 ms encendido / 450 ms apagado a 185 (antes 400/300 a 200).
- **Mensajes inválidos:** un primer carácter que no sea `0`–`3` se ignora (antes se
  tomaba como nivel 4, vibración continua). El nivel 4 ya no existe.
- **Diagnóstico en pantalla:** nivel, fuerza, conexión, batería (si el StickS3 la informa)
  y la causa del último reinicio.

Compatibilidad: el firmware anterior sólo lee el primer carácter, así que funciona con
el Python nuevo (ignora la fuerza). El firmware nuevo funciona con el Python anterior.

## Problema: con batería, en nivel 3 se desconecta y vuelve a conectarse

Hipótesis (sin medir todavía): al encender el motor con fuerza, la corriente sube de
golpe, el voltaje de la batería cae y el ESP32 se reinicia («brownout»). Al reiniciarse
vuelve a anunciarse por Bluetooth y Python lo reconecta solo. El firmware anterior ya
decía «Máximo 210 para evitar reinicios».

Cómo comprobarlo con esta versión:

1. Cargar el firmware y desconectar el powerbank.
2. Desde `web`: `python manage.py check_bracelet --real`, o una sesión con pulsera real.
3. Provocar el nivel 3 con distintas fuerzas (deslizador de la interfaz).
4. Si se reinicia, la pantalla dirá **«Ult. reinicio: VOLTAJE BAJO»**: confirma que es la
   alimentación. Anotar con qué fuerza deja de pasar.

Si sigue pasando, en este orden:

- Bajar `PATRONES[3].intensidad` o subir su tiempo apagado.
- Bajar `RAMPA_PASO` (arranque más lento).
- Subir `PWM_MINIMO` si el motor no gira con fuerza 1, o bajarlo si consume demasiado.
- Hardware: un condensador (p. ej. 100–470 µF) entre 5 V y GND del Grove ayuda a absorber
  el pico de arranque. El documento oficial pide alimentación externa e intensidad limitada.

Este firmware **no se ha compilado ni probado** en la pulsera: lo debe cargar y probar
quien la tenga.
