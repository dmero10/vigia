# Firmware de la pulsera M5StickS3

`pulsera_sticks3_final/pulsera_sticks3_final.ino`: firmware del compañero, sin cambios.
Se abre y se carga con Arduino IDE (la carpeta debe llamarse igual que el `.ino`).

Datos que usa Python (`web/alerts/ble_client.py`):

- Nombre BLE: `PulseraSomnolencia`
- Servicio: `12345678-1234-1234-1234-1234567890ab`
- Característica (escritura con respuesta): `abcd1234-5678-90ab-cdef-1234567890ab`
- Mensaje: el nivel como un dígito de texto (`"0"` a `"3"`). Cualquier mensaje
  reinicia el contador de 3 s (RF-09), así que el latido reenvía el nivel.
- Motor U059 en el Grove G9 con salida de 5 V habilitada.

Notas:
- El firmware acepta niveles 0-4 (versión anterior de los requisitos); Python sólo envía 0-3.
- La pantalla sólo se redibuja al cambiar el nivel, así que puede seguir diciendo
  «Esperando...» aunque ya esté conectada.
- `diagnostico_motor.ino` (no incluido) usaba el pin G2, que es incorrecto.
