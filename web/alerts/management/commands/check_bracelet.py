"""Prueba el envío de niveles a la pulsera, sin cámara.

Uso:
    python manage.py check_bracelet          (pulsera simulada)
    python manage.py check_bracelet --real   (pulsera M5StickS3 por Bluetooth)
"""

import time

from django.core.management.base import BaseCommand, CommandError

from alerts.bracelet import BraceletError, SimulatedBracelet
from alerts.services import AlertSender, create_bracelet

STEP_SECONDS = 2.0
DISCONNECT_SECONDS = 3.0
REAL_STEP_SECONDS = 4.0  # Tiempo para sentir el patrón de cada nivel.
CONNECT_WAIT_SECONDS = 30.0
SILENCE_SECONDS = 6
MAX_LATENCY_MS = 1000  # RF-07 / RNF-01


def wait_for(condition, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


class Command(BaseCommand):
    help = 'Envía los niveles 0-3 a la pulsera simulada o a la real (--real).'

    def add_arguments(self, parser):
        parser.add_argument('--real', action='store_true',
                            help='Usa la pulsera real por Bluetooth en vez de la simulada.')

    def handle(self, *args, **options):
        if options['real']:
            self._check_real()
        else:
            self._check_simulated()

    # ---------- Pulsera real ----------

    def _check_real(self):
        bracelet = create_bracelet('ble')
        sender = AlertSender(bracelet)
        self.stdout.write('Buscando la pulsera por Bluetooth (puede tardar unos segundos)...')
        sender.start()
        try:
            if not wait_for(lambda: sender.status().sent_level == 0, CONNECT_WAIT_SECONDS):
                raise CommandError(
                    f'No se pudo conectar: {sender.status().last_error or "sin respuesta"}'
                )
            self.stdout.write(self.style.SUCCESS(f'Conectada ({bracelet.address}).'))

            latencies = []
            for level in (1, 2, 3, 0):
                sender.set_level(level)
                if not wait_for(lambda: sender.status().sent_level == level, 3.0):
                    raise CommandError(f'No llegó el nivel {level}: {sender.status().last_error}')
                latency = sender.status().latency_ms
                latencies.append(latency)
                self.stdout.write(f'Nivel {level} enviado en {latency:.0f} ms. '
                                  f'Observe el motor {REAL_STEP_SECONDS:.0f} s...')
                time.sleep(REAL_STEP_SECONDS)
                if not sender.status().connected:
                    raise CommandError(f'Se desconectó: {sender.status().last_error}')
        finally:
            sender.stop()

        worst = max(latencies)
        text = f'Latencia máxima: {worst:.0f} ms (máximo {MAX_LATENCY_MS} ms)'
        if worst < MAX_LATENCY_MS:
            self.stdout.write(self.style.SUCCESS(f'[OK] {text}'))
        else:
            self.stdout.write(self.style.ERROR(f'[NO CUMPLE] {text}'))

        self._check_silence(bracelet)

    def _check_silence(self, bracelet):
        """RF-09: se envía nivel 2 y luego nada; el motor debe pararse a los 3 s."""
        self.stdout.write('Prueba RF-09: nivel 2 y después silencio.')
        try:
            bracelet.connect()
            bracelet.send_level(2)
            for second in range(1, SILENCE_SECONDS + 1):
                time.sleep(1)
                note = '  <- el motor debería pararse a partir de aquí' if second == 3 else ''
                self.stdout.write(f'  {second} s sin mensajes{note}')
        except BraceletError as error:
            raise CommandError(str(error)) from error
        finally:
            bracelet.close()
        self.stdout.write('¿El motor se detuvo cerca de los 3 s? Anótelo como resultado de RF-09.')

    # ---------- Pulsera simulada ----------

    def _check_simulated(self):
        bracelet = SimulatedBracelet()
        sender = AlertSender(bracelet)
        sender.start()
        start = time.monotonic()
        try:
            for level in (0, 1, 2, 3, 0):
                sender.set_level(level)
                self._show(sender, bracelet, start, f'Se pide nivel {level}')

            self.stdout.write('-- Se apaga la pulsera simulada --')
            bracelet.set_available(False)
            self._show(sender, bracelet, start, 'Pulsera apagada', DISCONNECT_SECONDS)

            self.stdout.write('-- Se vuelve a encender --')
            bracelet.set_available(True)
            sender.set_level(2)
            self._show(sender, bracelet, start, 'Se pide nivel 2', 4.0)
        finally:
            sender.stop()

        messages = bracelet.messages()
        levels = sum(1 for message in messages if message.kind == 'level')
        self.stdout.write(f'Mensajes recibidos: {levels} de nivel y '
                          f'{len(messages) - levels} latidos.')
        self.stdout.write(f'Último mensaje al detener: nivel {messages[-1].level}.')

    def _show(self, sender, bracelet, start, title, seconds=STEP_SECONDS):
        self.stdout.write(title)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(0.5)
            status = sender.status()
            latency = f'{status.latency_ms:.1f} ms' if status.latency_ms is not None else '-'
            state = 'conectada' if status.connected else f'desconectada ({status.last_error})'
            self.stdout.write(
                f'  t={time.monotonic() - start:4.1f} s | {state} | '
                f'enviado {status.sent_level} | motor {bracelet.vibrating_level} | '
                f'latencia {latency}'
            )
