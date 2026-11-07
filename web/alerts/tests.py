"""Pruebas de M3. La pulsera real se prueba con un Bluetooth falso (sin hardware)."""

import asyncio
import time
from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from alerts.ble_client import (
    LEVEL_CHARACTERISTIC_UUID,
    SERVICE_UUID,
    BleBracelet,
    encode_level,
)
from alerts.bracelet import BraceletError, SimulatedBracelet
from alerts.services import AlertSender, create_bracelet
from bleak.exc import BleakBluetoothNotAvailableError


def wait_until(condition, timeout=2.0):
    """Espera a que se cumpla `condition` (el envío ocurre en otro hilo)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.005)
    return False


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SimulatedBraceletTests(SimpleTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bracelet = SimulatedBracelet(clock=self.clock)

    def test_send_without_connection_raises(self):
        with self.assertRaises(BraceletError):
            self.bracelet.send_level(1)

    def test_unavailable_bracelet_cannot_connect(self):
        self.bracelet.set_available(False)
        with self.assertRaisesMessage(BraceletError, 'no está disponible'):
            self.bracelet.connect()

    def test_turning_off_disconnects(self):
        self.bracelet.connect()
        self.bracelet.set_available(False)
        self.assertFalse(self.bracelet.is_connected)
        with self.assertRaises(BraceletError):
            self.bracelet.send_heartbeat(0)

    def test_records_messages(self):
        self.bracelet.connect()
        self.bracelet.send_level(2)
        self.bracelet.send_heartbeat(2)
        self.assertEqual([(m.kind, m.level) for m in self.bracelet.messages()],
                         [('level', 2), ('heartbeat', 2)])

    def test_motor_turns_off_after_three_seconds_without_signal(self):
        self.assertEqual(self.bracelet.vibrating_level, 0)
        self.bracelet.connect()
        self.bracelet.send_level(3)
        self.clock.now = 3.0
        self.assertEqual(self.bracelet.vibrating_level, 3)
        self.clock.now = 3.01
        self.assertEqual(self.bracelet.vibrating_level, 0)

    def test_heartbeat_keeps_motor_on(self):
        self.bracelet.connect()
        self.bracelet.send_level(2)
        self.clock.now = 2.0
        self.bracelet.send_heartbeat(2)
        self.clock.now = 4.5
        self.assertEqual(self.bracelet.vibrating_level, 2)


class AlertSenderTests(SimpleTestCase):
    def setUp(self):
        self.bracelet = SimulatedBracelet()
        self.sender = AlertSender(self.bracelet, heartbeat_interval=0.05, retry_interval=0.05)
        self.addCleanup(self.sender.stop)

    def start_connected(self):
        self.sender.start()
        self.assertTrue(wait_until(lambda: self.sender.status().sent_level == 0))

    def level_messages(self):
        return [m.level for m in self.bracelet.messages() if m.kind == 'level']

    def test_connects_and_sends_initial_level(self):
        self.start_connected()
        status = self.sender.status()
        self.assertTrue(status.connected)
        self.assertIsNone(status.last_error)

    def test_level_change_is_sent_quickly(self):
        self.start_connected()
        self.sender.set_level(2)
        self.assertTrue(wait_until(lambda: self.sender.status().sent_level == 2))
        self.assertEqual(self.level_messages(), [0, 2])
        self.assertLess(self.sender.status().latency_ms, 1000)  # RF-07

    def test_set_level_does_not_wait_for_the_bracelet(self):
        # Sin iniciar el hilo, set_level debe volver de inmediato.
        start = time.monotonic()
        self.sender.set_level(3)
        self.assertLess(time.monotonic() - start, 0.05)
        self.assertEqual(self.sender.status().level, 3)

    def test_repeated_level_is_sent_once(self):
        self.start_connected()
        for _ in range(5):
            self.sender.set_level(1)
        self.assertTrue(wait_until(lambda: self.sender.status().sent_level == 1))
        time.sleep(0.15)
        self.assertEqual(self.level_messages(), [0, 1])

    def test_sends_heartbeats_without_changes(self):
        self.start_connected()
        self.assertTrue(wait_until(
            lambda: sum(m.kind == 'heartbeat' for m in self.bracelet.messages()) >= 3
        ))
        self.assertEqual(self.level_messages(), [0])

    def test_invalid_level_raises(self):
        for level in (-1, 4):
            with self.assertRaises(ValueError):
                self.sender.set_level(level)

    def test_retries_until_bracelet_is_available(self):
        self.bracelet.set_available(False)
        self.sender.start()
        self.assertTrue(wait_until(lambda: self.sender.status().last_error is not None))
        self.assertFalse(self.sender.status().connected)
        self.bracelet.set_available(True)
        self.assertTrue(wait_until(lambda: self.sender.status().connected))

    def test_reconnects_and_resends_current_level(self):
        self.start_connected()
        self.sender.set_level(2)
        self.assertTrue(wait_until(lambda: self.sender.status().sent_level == 2))
        self.bracelet.set_available(False)
        self.assertTrue(wait_until(lambda: not self.sender.status().connected))
        self.bracelet.set_available(True)
        self.assertTrue(wait_until(lambda: self.level_messages() == [0, 2, 2]))

    def test_time_disconnected_is_not_counted_as_latency(self):
        self.bracelet.set_available(False)
        self.sender.start()
        self.sender.set_level(2)
        time.sleep(0.2)
        self.bracelet.set_available(True)
        self.assertTrue(wait_until(lambda: self.sender.status().sent_level == 2))
        self.assertIsNone(self.sender.status().latency_ms)

    def test_stop_sends_zero_and_disconnects(self):
        self.start_connected()
        self.sender.set_level(3)
        self.assertTrue(wait_until(lambda: self.sender.status().sent_level == 3))
        self.sender.stop()
        self.assertEqual(self.bracelet.messages()[-1].level, 0)
        self.assertFalse(self.bracelet.is_connected)
        self.assertFalse(self.sender.status().connected)

    def test_retry_now_skips_the_wait(self):
        self.bracelet.set_available(False)
        slow = AlertSender(self.bracelet, retry_interval=60)
        slow.start()
        self.addCleanup(slow.stop)
        self.assertTrue(wait_until(lambda: slow.status().last_error is not None))
        self.bracelet.set_available(True)
        slow.retry_now()
        self.assertTrue(wait_until(lambda: slow.status().connected, timeout=1))

    def test_stop_while_disconnected_is_quick(self):
        self.bracelet.set_available(False)
        slow = AlertSender(self.bracelet, retry_interval=60)
        slow.start()
        start = time.monotonic()
        slow.stop()
        self.assertLess(time.monotonic() - start, 1)

    def test_stop_twice_is_safe(self):
        self.start_connected()
        self.sender.stop()
        self.sender.stop()


class CreateBraceletTests(SimpleTestCase):
    @override_settings(BRACELET_MODE='simulated')
    def test_simulated_mode(self):
        self.assertIsInstance(create_bracelet(), SimulatedBracelet)

    def test_ble_mode(self):
        self.assertIsInstance(create_bracelet('ble'), BleBracelet)

    def test_unknown_mode(self):
        with self.assertRaises(ImproperlyConfigured):
            create_bracelet('otro')


class FakeBleakClient:
    """Imita a BleakClient: guarda lo que se escribe."""

    def __init__(self, device, timeout=None, fail_write=None):
        self.device = device
        self.is_connected = False
        self.writes = []
        self.fail_write = fail_write

    async def connect(self):
        self.is_connected = True

    async def write_gatt_char(self, uuid, data, response=None):
        if self.fail_write:
            raise self.fail_write
        self.writes.append((uuid, bytes(data), response))

    async def disconnect(self):
        self.is_connected = False


def advertisement(name=None, uuids=()):
    return SimpleNamespace(local_name=name, service_uuids=list(uuids))


class EncodeLevelTests(SimpleTestCase):
    def test_levels_are_single_ascii_digits(self):
        self.assertEqual([encode_level(level) for level in range(4)], [b'0', b'1', b'2', b'3'])

    def test_invalid_levels_are_rejected(self):
        # El firmware tomaría cualquier otro carácter como nivel 4 (vibración continua).
        for level in (-1, 4, 10):
            with self.assertRaises(ValueError):
                encode_level(level)


class BleBraceletTests(SimpleTestCase):
    def setUp(self):
        self.device = SimpleNamespace(name='PulseraSomnolencia', address='AA:BB:CC:DD:EE:FF')
        self.clients = []
        self.find = mock.AsyncMock(return_value=self.device)
        patches = [
            mock.patch('alerts.ble_client.BleakScanner.find_device_by_filter', self.find),
            mock.patch('alerts.ble_client.BleakClient', self.make_client),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.bracelet = BleBracelet(scan_timeout=0.1, connect_timeout=0.1, write_timeout=0.5)
        self.addCleanup(self.bracelet.close)
        self.fail_write = None

    def make_client(self, device, timeout=None):
        client = FakeBleakClient(device, timeout, self.fail_write)
        self.clients.append(client)
        return client

    def test_connect_and_send_level(self):
        self.bracelet.connect()
        self.assertTrue(self.bracelet.is_connected)
        self.assertEqual(self.bracelet.address, 'AA:BB:CC:DD:EE:FF')
        self.bracelet.send_level(2)
        self.bracelet.send_heartbeat(2)
        expected = (LEVEL_CHARACTERISTIC_UUID, b'2', True)
        self.assertEqual(self.clients[0].writes, [expected, expected])

    def test_bracelet_not_found(self):
        self.find.return_value = None
        with self.assertRaisesMessage(BraceletError, 'No se encontró la pulsera'):
            self.bracelet.connect()
        self.assertFalse(self.bracelet.is_connected)

    def test_send_without_connection_raises(self):
        with self.assertRaisesMessage(BraceletError, 'desconectada'):
            self.bracelet.send_level(1)

    def test_bluetooth_off_has_clear_message(self):
        self.find.side_effect = BleakBluetoothNotAvailableError('Bluetooth radio is not powered on.', None)
        with self.assertRaisesMessage(BraceletError, 'Bluetooth de Windows está apagado'):
            self.bracelet.connect()

    def test_bluetooth_errors_become_bracelet_errors(self):
        self.fail_write = OSError('radio apagada')
        self.bracelet.connect()
        with self.assertRaisesMessage(BraceletError, 'radio apagada'):
            self.bracelet.send_level(1)

    def test_slow_write_times_out(self):
        self.bracelet.connect()

        async def slow_write(*args, **kwargs):
            await asyncio.sleep(5)

        self.clients[0].write_gatt_char = slow_write
        start = time.monotonic()
        with self.assertRaisesMessage(BraceletError, 'no respondió'):
            self.bracelet.send_level(1)
        self.assertLess(time.monotonic() - start, 2)

    def test_lost_connection_is_detected(self):
        self.bracelet.connect()
        self.clients[0].is_connected = False  # Por ejemplo, la pulsera se apagó.
        self.assertFalse(self.bracelet.is_connected)
        with self.assertRaises(BraceletError):
            self.bracelet.send_level(1)

    def test_close_disconnects_and_allows_reconnecting(self):
        self.bracelet.connect()
        self.bracelet.close()
        self.assertFalse(self.clients[0].is_connected)
        self.bracelet.connect()
        self.assertEqual(len(self.clients), 2)

    def test_matches_by_name_or_service(self):
        match = self.bracelet._matches
        unnamed = SimpleNamespace(name=None)
        self.assertTrue(match(self.device, advertisement()))
        self.assertTrue(match(unnamed, advertisement(name='PulseraSomnolencia')))
        self.assertTrue(match(unnamed, advertisement(uuids=[SERVICE_UUID.upper()])))
        self.assertFalse(match(SimpleNamespace(name='Otro'), advertisement(name='Otro')))

    def test_works_with_the_sender(self):
        sender = AlertSender(self.bracelet, heartbeat_interval=0.05, retry_interval=0.05)
        sender.start()
        self.addCleanup(sender.stop)
        self.assertTrue(wait_until(lambda: sender.status().sent_level == 0))
        sender.set_level(3)
        self.assertTrue(wait_until(lambda: sender.status().sent_level == 3))
        sender.stop()
        sent = [data for _, data, _ in self.clients[0].writes]
        self.assertEqual(sent[-1], b'0')
        self.assertIn(b'3', sent)
