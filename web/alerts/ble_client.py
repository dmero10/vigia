"""M3 - Infraestructura: pulsera real por Bluetooth LE (Bleak).

Los datos salen del firmware `firmware/pulsera_sticks3_final/pulsera_sticks3_final.ino`.
Si cambia el firmware, sólo hay que actualizar las constantes de este archivo.

Bleak es asíncrono (asyncio). Para que el resto del sistema lo use como funciones
normales, cada pulsera tiene su propio hilo con un bucle de asyncio.
"""

import asyncio
import threading

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakBluetoothNotAvailableError

from alerts.bracelet import Bracelet, BraceletError

DEVICE_NAME = 'PulseraSomnolencia'
SERVICE_UUID = '12345678-1234-1234-1234-1234567890ab'
LEVEL_CHARACTERISTIC_UUID = 'abcd1234-5678-90ab-cdef-1234567890ab'

SCAN_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 1.0  # RF-07: el nivel debe llegar en menos de 1 s.
DISCONNECT_TIMEOUT_S = 3.0


def encode_level(level):
    """El firmware lee el primer carácter como dígito: el nivel 2 se envía como "2"."""
    if level not in range(4):
        # Un carácter que no sea dígito, el firmware lo tomaría como nivel 4 (vibración continua).
        raise ValueError(f'Nivel inválido: {level}. Debe ser 0, 1, 2 o 3.')
    return str(level).encode('ascii')


class BleBracelet(Bracelet):
    """Pulsera M5StickS3 real."""

    name = 'Pulsera real (Bluetooth)'

    def __init__(self, device_name=DEVICE_NAME, scan_timeout=SCAN_TIMEOUT_S,
                 connect_timeout=CONNECT_TIMEOUT_S, write_timeout=WRITE_TIMEOUT_S):
        self.device_name = device_name
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self.write_timeout = write_timeout
        self.address = None
        self._client = None
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name='ble-loop', daemon=True
        )
        self._loop_thread.start()

    @property
    def is_connected(self):
        client = self._client
        return client is not None and client.is_connected

    def connect(self):
        if self.is_connected:
            return
        self._run(self._connect(), self.scan_timeout + self.connect_timeout + 2)

    def send_level(self, level):
        data = encode_level(level)
        client = self._client
        if client is None or not client.is_connected:
            raise BraceletError('La pulsera está desconectada.')
        # El firmware sólo permite escritura con respuesta (PROPERTY_WRITE).
        self._run(
            client.write_gatt_char(LEVEL_CHARACTERISTIC_UUID, data, response=True),
            self.write_timeout,
        )

    def close(self):
        client, self._client = self._client, None
        if client is not None:
            try:
                self._run(client.disconnect(), DISCONNECT_TIMEOUT_S)
            except BraceletError:
                pass  # Ya estaba desconectada o no responde: no hay nada más que hacer.

    async def _connect(self):
        device = await BleakScanner.find_device_by_filter(self._matches, timeout=self.scan_timeout)
        if device is None:
            raise BraceletError(
                f'No se encontró la pulsera «{self.device_name}». Revise que esté '
                'encendida, cerca y que el Bluetooth de Windows esté activado.'
            )
        client = BleakClient(device, timeout=self.connect_timeout)
        await client.connect()
        self.address = device.address
        self._client = client

    def _matches(self, device, advertisement):
        # Se busca por nombre o por servicio: Windows no siempre recibe el nombre.
        names = (device.name, advertisement.local_name)
        uuids = [uuid.lower() for uuid in advertisement.service_uuids]
        return self.device_name in names or SERVICE_UUID in uuids

    def _run(self, coroutine, timeout):
        """Ejecuta una corrutina en el bucle de la pulsera y espera su resultado."""
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout)
        except BraceletError:
            raise
        except TimeoutError as error:
            future.cancel()
            raise BraceletError(f'La pulsera no respondió en {timeout:.1f} s.') from error
        except BleakBluetoothNotAvailableError as error:
            raise BraceletError(
                'El Bluetooth de Windows está apagado. Actívelo en '
                'Configuración > Bluetooth y dispositivos.'
            ) from error
        except Exception as error:  # noqa: BLE001 - cualquier fallo de Bluetooth
            # Se convierte todo a BraceletError para que el hilo de envío no se caiga
            # y pueda reintentar la conexión.
            raise BraceletError(f'Error de Bluetooth: {type(error).__name__}: {error}') from error
