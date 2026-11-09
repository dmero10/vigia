"""Comprueba que el microservicio de registro M4 responde y muestra las últimas sesiones.

Uso (con M4 encendido en otra terminal):
    python manage.py check_registry
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from monitoring.registry_client import RegistryClient, RegistryError


class Command(BaseCommand):
    help = 'Comprueba la conexión con el microservicio de registro (M4).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=5, help='Sesiones a mostrar.')

    def handle(self, *args, limit, **options):
        client = RegistryClient(settings.REGISTRY_URL)
        self.stdout.write(f'M4 en {settings.REGISTRY_URL}')
        try:
            health = client.health()
            sessions = client.sessions(limit)
        except RegistryError as error:
            raise CommandError(
                f'{error}\n¿Está encendido? En otra terminal: cd registry_service y '
                'python -m uvicorn app.main:app --port 8001'
            ) from error
        self.stdout.write(self.style.SUCCESS(
            f"[OK] {health['service']} {health['version']} · base de datos {health['database']}"
        ))
        if not sessions:
            self.stdout.write('Todavía no hay sesiones registradas.')
            return
        self.stdout.write(f'Últimas {len(sessions)} sesiones:')
        for session in sessions:
            summary = client.summary(session['id'])
            self.stdout.write(
                f"  {session['started_at'][:19]} · {session['status']:8} · "
                f"{session['event_count']} eventos · {summary['alert_count']} alertas · "
                f"nivel máx. {summary['max_level']} · {summary['duration_s']:.0f} s"
            )
