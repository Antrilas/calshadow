"""Gemeinsame Test-Fixtures.

Unit-Tests laufen ohne Server. Integrationstests brauchen einen CalDAV-Server ohne Anmeldung
(Radicale mit `auth.type = none`) unter RADICALE_URL, Standard: http://127.0.0.1:5232.
Der private und der berufliche Kalender werden über zwei verschiedene Hostnamen desselben Servers
angesprochen (127.0.0.1 bzw. localhost), weil CalShadow gleiche Hosts für Quelle und Ziel ablehnt.
"""

import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import backup, db  # noqa: E402

RADICALE_URL = os.environ.get("RADICALE_URL", "http://127.0.0.1:5232").rstrip("/")
SOURCE_BASE = RADICALE_URL + "/"
TARGET_BASE = RADICALE_URL.replace("127.0.0.1", "localhost") + "/"


@pytest.fixture(autouse=True)
def app_env(tmp_path, monkeypatch):
    """Eigene Datenbank und eigenes Sicherungsverzeichnis pro Test."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "calshadow.db"))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path / "backups"))
    db.init()
    yield tmp_path


def radicale_available() -> bool:
    import requests
    try:
        return requests.get(SOURCE_BASE, timeout=2).status_code < 500
    except Exception:
        return False


@pytest.fixture
def calendars():
    """Legt einen privaten und einen beruflichen Testkalender an und konfiguriert CalShadow dafür."""
    if not radicale_available():
        pytest.skip(f"Kein CalDAV-Testserver unter {RADICALE_URL}")
    import caldav

    admin = caldav.DAVClient(SOURCE_BASE, username="u", password="p")
    principal = admin.principal()
    suffix = uuid.uuid4().hex[:8]
    src = principal.make_calendar(name="privat", cal_id=f"privat-{suffix}")
    dst = principal.make_calendar(name="arbeit", cal_id=f"arbeit-{suffix}")
    target_url = str(dst.url).replace("127.0.0.1", "localhost")
    db.save_settings({
        "source_url": SOURCE_BASE, "source_username": "u", "source_password": "p",
        "source_calendars": str(src.url),
        "target_url": TARGET_BASE, "target_username": "u", "target_password": "p",
        "target_calendar": target_url,
        "backup_enabled": False, "weekdays": [0, 1, 2, 3, 4, 5, 6],
    })
    yield src, dst
    for cal in (src, dst):
        try:
            cal.delete()
        except Exception:
            pass


def day(offset: int) -> str:
    """Datum relativ zu heute im iCalendar-Format (JJJJMMTT)."""
    return (date.today() + timedelta(days=offset)).strftime("%Y%m%d")


def add_event(cal, uid: str, start: str, end: str, extra: str = "", summary: str = "Privat") -> None:
    """Legt einen Termin an; start/end als iCalendar-Wert inkl. Präfix, z. B. ':20261102T100000'."""
    cal.save_event(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//DE\nBEGIN:VEVENT\n"
        f"UID:{uid}\nDTSTAMP:20260101T000000Z\nDTSTART{start}\nDTEND{end}\nSUMMARY:{summary}\n{extra}"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
