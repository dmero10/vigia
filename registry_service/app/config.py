"""Configuración de M4. Se puede cambiar con variables de entorno, sin tocar el código."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent

# Base de datos propia de M4 (Django nunca la lee directamente).
DB_PATH = Path(os.environ.get('VIGIA_REGISTRY_DB', BASE_DIR / 'data' / 'registry.sqlite3'))

# Zona horaria con la que se muestran las horas en el CSV.
TIME_ZONE = ZoneInfo(os.environ.get('VIGIA_TIME_ZONE', 'America/Guayaquil'))

API_VERSION = '1.0.0'
