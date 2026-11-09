"""Capa de acceso a datos: SQLite. Es la única parte de M4 que conoce las tablas.

Las fechas se guardan como texto ISO en UTC con formato fijo, para que ordenar
por texto sea lo mismo que ordenar por tiempo.
"""

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    bracelet_mode TEXT,
    threshold     REAL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    level           INTEGER NOT NULL CHECK (level BETWEEN 0 AND 3),
    degraded_reason TEXT,
    ear             REAL,
    mar             REAL,
    closed_ms       INTEGER NOT NULL DEFAULT 0,
    yawn_count      INTEGER NOT NULL DEFAULT 0,
    received_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_session ON events (session_id, at);
"""

_DATE_FORMAT = '%Y-%m-%dT%H:%M:%S.%f+00:00'


def to_db(value):
    return value.astimezone(timezone.utc).strftime(_DATE_FORMAT) if value else None


def from_db(value):
    return datetime.fromisoformat(value) if value else None


def _now():
    return to_db(datetime.now(timezone.utc))


class Repository:
    def __init__(self, db_path):
        self.db_path = Path(db_path)

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.db_path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys = ON')
            with connection:  # Confirma al salir o deshace si hubo un error.
                yield connection

    def init_schema(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    def ping(self):
        with self._connection() as connection:
            connection.execute('SELECT 1')

    # ---------- Sesiones ----------

    def insert_session(self, session_id, started_at, bracelet_mode):
        """Devuelve False si la sesión ya existía (reintento)."""
        with self._connection() as connection:
            cursor = connection.execute(
                'INSERT OR IGNORE INTO sessions (id, started_at, bracelet_mode, created_at) '
                'VALUES (?, ?, ?, ?)',
                (session_id, to_db(started_at), bracelet_mode, _now()),
            )
            return cursor.rowcount == 1

    def finish_session(self, session_id, ended_at, threshold):
        """Devuelve False si la sesión no existe."""
        with self._connection() as connection:
            cursor = connection.execute(
                'UPDATE sessions SET ended_at = ?, threshold = COALESCE(?, threshold) WHERE id = ?',
                (to_db(ended_at), threshold, session_id),
            )
            return cursor.rowcount == 1

    def get_session(self, session_id):
        with self._connection() as connection:
            row = connection.execute(
                f'{_SESSION_QUERY} WHERE s.id = ? GROUP BY s.id', (session_id,)
            ).fetchone()
        return _session(row) if row else None

    def list_sessions(self, limit):
        with self._connection() as connection:
            rows = connection.execute(
                f'{_SESSION_QUERY} GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?', (limit,)
            ).fetchall()
        return [_session(row) for row in rows]

    # ---------- Eventos ----------

    def insert_events(self, session_id, events):
        """Guarda los eventos nuevos e ignora los repetidos. Devuelve (nuevos, repetidos)."""
        received_at = _now()
        accepted = 0
        with self._connection() as connection:
            for event in events:
                cursor = connection.execute(
                    'INSERT OR IGNORE INTO events (id, session_id, at, level, degraded_reason, '
                    'ear, mar, closed_ms, yawn_count, received_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (event.id, session_id, to_db(event.at), event.level, event.degraded_reason,
                     event.ear, event.mar, event.closed_ms, event.yawn_count, received_at),
                )
                accepted += cursor.rowcount
        return accepted, len(events) - accepted

    def list_events(self, session_id):
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT id, at, level, degraded_reason, ear, mar, closed_ms, yawn_count '
                'FROM events WHERE session_id = ? ORDER BY at, received_at',
                (session_id,),
            ).fetchall()
        return [{**dict(row), 'at': from_db(row['at'])} for row in rows]


_SESSION_QUERY = (
    'SELECT s.id, s.started_at, s.ended_at, s.bracelet_mode, s.threshold, '
    'COUNT(e.id) AS event_count '
    'FROM sessions s LEFT JOIN events e ON e.session_id = s.id'
)


def _session(row):
    data = dict(row)
    data['started_at'] = from_db(data['started_at'])
    data['ended_at'] = from_db(data['ended_at'])
    return data
