"""SQLite-Persistenz: Einstellungen, Sync-Läufe, Logs und Blocker-Snapshot."""

import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/calshadow.db")

DEFAULT_SETTINGS = {
    # Privater Kalender (Quelle, nur lesend)
    "source_name": "Privat",
    "source_url": "https://posteo.de:8443/",
    "source_username": "",
    "source_password": "",
    "source_calendars": "",  # eine Kalender-URL pro Zeile
    "source_mode": "events",  # events = Termine lesen | freebusy = nur Frei/Belegt (keine Termininhalte)
    # Beruflicher Kalender (Ziel)
    "target_name": "Arbeit",
    "target_url": "",
    "target_username": "",
    "target_password": "",
    "target_calendar": "",
    "target_uid_filter": True,  # nur eigene Blocker abfragen (serverseitiger UID-Filter)
    # Regeln
    "days_back": 0,    # Tage in die Vergangenheit (0 = ab heute)
    "days_ahead": 60,
    "work_start": "08:00",
    "work_end": "18:00",
    "filter_mode": "strict",  # strict | clip
    "weekdays": [0, 1, 2, 3, 4, 5, 6],
    "include_all_day": False,
    "skip_transparent": True,
    "skip_tentative": False,
    "max_deletions": 30,
    # Darstellung
    "blocker_title": "Privater Blocker",
    # optionaler, fester Text für alle Blocker (nie aus dem privaten Termin); leer = keine Beschreibung
    "blocker_description": (
        "Dieser Termin wurde von CalShadow automatisch aus einem privaten Kalender übernommen.\n"
        "Nur der Schatten eines Termins. Der Rest bleibt privat. – CalShadow"
    ),
    "blocker_class": "PRIVATE",  # PUBLIC | PRIVATE | CONFIDENTIAL
    # Zeitplan
    "enabled": False,
    "interval_minutes": 15,
    "timezone": os.environ.get("TZ", "Europe/Berlin"),
    "log_retention_days": 30,
    "log_event_titles": False,  # nur zur Fehlersuche: Titel privater Termine in Logs ausgeben
    # Sicherung des beruflichen Kalenders vor einem Lauf
    "backup_enabled": True,
    "backup_min_interval_minutes": 60,  # höchstens eine Sicherung pro Intervall (0 = vor jedem Lauf)
    "backup_keep_last": 10,
    "backup_keep_hourly": 72,
    "backup_hourly_skip_weekend": True,
    "backup_keep_daily": 28,
    # Monitoring (Homepage)
    "homepage_token": "",  # Lese-Token für /api/homepage (wird beim ersten Start erzeugt)
    "stale_factor": 3,     # "Veraltet", wenn letzter erfolgreicher Sync älter als Faktor × Intervall
    # Uptime Kuma (Push-Monitore, leer = aus)
    "kuma_backup_push_url": "",
    "kuma_sync_push_url": "",
    # Geheimer Schlüssel zum Signieren der eigenen Blocker (wird beim ersten Start erzeugt)
    "instance_secret": "",
}

SECRET_KEYS = {"source_password", "target_password", "instance_secret", "homepage_token"}

_lock = threading.RLock()


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started TEXT NOT NULL,
                finished TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                trigger TEXT NOT NULL DEFAULT 'schedule',
                dry_run INTEGER NOT NULL DEFAULT 0,
                source_events INTEGER DEFAULT 0,
                created INTEGER DEFAULT 0,
                updated INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                unchanged INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_logs_run ON logs(run_id);
            CREATE TABLE IF NOT EXISTS blockers (
                uid TEXT PRIMARY KEY,
                start TEXT NOT NULL,
                end TEXT NOT NULL
            );
            """
        )
    current = get_settings()
    if not current["instance_secret"]:
        save_settings({"instance_secret": secrets.token_hex(32)})
    if not current["homepage_token"]:
        save_settings({"homepage_token": secrets.token_urlsafe(32)})


# ---------- Settings ----------

def get_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    with conn() as c:
        for row in c.execute("SELECT key, value FROM settings"):
            if row["key"] in s:
                s[row["key"]] = json.loads(row["value"])
    return s


def save_settings(values: dict):
    with conn() as c:
        for k, v in values.items():
            if k not in DEFAULT_SETTINGS:
                continue
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)),
            )


# ---------- Runs & Logs ----------

def start_run(trigger: str, dry_run: bool) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs(started, trigger, dry_run) VALUES(?, ?, ?)",
            (now_iso(), trigger, int(dry_run)),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str, stats: dict, error: str | None = None):
    with conn() as c:
        c.execute(
            """UPDATE runs SET finished=?, status=?, source_events=?, created=?, updated=?,
               deleted=?, unchanged=?, skipped=?, error=? WHERE id=?""",
            (
                now_iso(), status,
                stats.get("source_events", 0), stats.get("created", 0),
                stats.get("updated", 0), stats.get("deleted", 0),
                stats.get("unchanged", 0), stats.get("skipped", 0),
                error, run_id,
            ),
        )


def add_log(run_id: int | None, level: str, message: str):
    with conn() as c:
        c.execute(
            "INSERT INTO logs(run_id, ts, level, message) VALUES(?, ?, ?, ?)",
            (run_id, now_iso(), level, message),
        )


RUN_KINDS = {
    "live": "dry_run=0 AND trigger!='Aufräumen'",
    "dry": "dry_run=1",
    "cleanup": "trigger='Aufräumen'",
}


def query_runs(kind: str = "", status: str = "", limit: int = 50, offset: int = 0) -> tuple[list[dict], int, dict]:
    """Läufe gefiltert nach Art und Status, seitenweise; dazu Gesamtzahl und Summen über alle Treffer."""
    where, args = ["1=1"], []
    if kind in RUN_KINDS:
        where.append(RUN_KINDS[kind])
    if status in ("success", "error", "running"):
        where.append("status=?")
        args.append(status)
    w = " AND ".join(where)
    with conn() as c:
        rows = [dict(r) for r in c.execute(f"SELECT * FROM runs WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?",
                                           (*args, limit, offset))]
        agg = dict(c.execute(
            f"""SELECT COUNT(*) AS total, SUM(status='success') AS ok, SUM(status='error') AS errors,
                   COALESCE(SUM(created),0) AS created, COALESCE(SUM(updated),0) AS updated,
                   COALESCE(SUM(deleted),0) AS deleted,
                   AVG(CASE WHEN finished IS NOT NULL
                       THEN (julianday(finished) - julianday(started)) * 86400 END) AS avg_seconds
                FROM runs WHERE {w}""", args).fetchone())
    return rows, agg["total"], agg


def get_runs(limit: int = 50) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


def get_run(run_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None


def last_real_run(status: str | None = None) -> dict | None:
    q = "SELECT * FROM runs WHERE dry_run=0 AND status!='running' AND trigger!='Aufräumen'"
    args = []
    if status:
        q += " AND status=?"
        args.append(status)
    with conn() as c:
        r = c.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return dict(r) if r else None


def get_logs(run_id: int | None = None, levels: list[str] | None = None,
             after_id: int = 0, limit: int = 1000) -> list[dict]:
    q = "SELECT * FROM logs WHERE id > ?"
    args: list = [after_id]
    if run_id is not None:
        q += " AND run_id = ?"
        args.append(run_id)
    if levels:
        q += f" AND level IN ({','.join('?' * len(levels))})"
        args.extend(levels)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with conn() as c:
        rows = [dict(r) for r in c.execute(q, args)]
    rows.reverse()
    return rows


def delete_run(run_id: int) -> bool:
    """Löscht einen abgeschlossenen Lauf samt Logs; ein laufender Sync kann nicht gelöscht werden."""
    with conn() as c:
        if c.execute("DELETE FROM runs WHERE id=? AND status != 'running'", (run_id,)).rowcount == 0:
            return False
        c.execute("DELETE FROM logs WHERE run_id=?", (run_id,))
    return True


def clear_logs() -> tuple[int, int]:
    """Löscht alle Logs und abgeschlossenen Läufe; ein gerade laufender Sync bleibt erhalten."""
    with conn() as c:
        logs = c.execute(
            "DELETE FROM logs WHERE run_id IS NULL OR run_id NOT IN (SELECT id FROM runs WHERE status='running')"
        ).rowcount
        runs = c.execute("DELETE FROM runs WHERE status != 'running'").rowcount
    return logs, runs


def prune(retention_days: int):
    with conn() as c:
        c.execute(
            "DELETE FROM logs WHERE ts < datetime('now', ?)", (f"-{int(retention_days)} days",)
        )
        c.execute(
            "DELETE FROM runs WHERE started < datetime('now', ?)", (f"-{int(retention_days)} days",)
        )


# ---------- Blocker-Snapshot (für Dashboard) ----------

def replace_blockers(items: list[tuple[str, str, str]]):
    with conn() as c:
        c.execute("DELETE FROM blockers")
        c.executemany("INSERT INTO blockers(uid, start, end) VALUES(?, ?, ?)", items)


def get_blockers() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM blockers ORDER BY start")]
