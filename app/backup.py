"""Sicherungen: automatisch vom beruflichen Kalender vor einem Sync-Lauf (mit Aufbewahrungsregeln)
sowie manuell vom beruflichen oder privaten Kalender (bleiben, bis sie gelöscht werden)."""

import json
import os
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from icalendar import Calendar

BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(os.path.dirname(os.environ.get("DB_PATH", "/data/x.db")), "backups"))
# backup-<kalendername>-<JJJJMMTT-hhmmss>-run<id>.zip (automatisch) bzw. …-manuell.zip
NAME_RE = re.compile(r"backup-[a-z0-9-]+-(\d{8}-\d{6})-(?:run(\d+)|manuell)\.zip")
SIDES = {"target": "beruflich", "source": "privat"}


def _object_data(obj) -> str:
    data = obj.data
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)


def slugify(name: str) -> str:
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40].strip("-")
    return s or "kalender"


def create_backup(cals: list, run_id: int | None, calendar_name: str, side: str = "target") -> dict:
    """Lädt alle Objekte (Termine und Aufgaben) der Kalender und speichert sie als ein ZIP.

    Das ZIP enthält jedes Objekt unverändert als einzelne .ics-Datei (objects/) sowie
    eine zusammengeführte kalender.ics, die sich direkt im Kalender (z. B. SOGo) importieren lässt.
    `run_id` None = manuelle Sicherung (wird von den Aufbewahrungsregeln nie gelöscht).
    Es werden nur lesende Abfragen (REPORT) gestellt.
    """
    t0 = time.monotonic()
    objects = []
    for cal in cals:
        objects += list(cal.search(event=True, expand=False))
        try:
            objects += list(cal.todos(include_completed=True))
        except Exception:
            pass  # Kalender ohne Aufgaben-Unterstützung

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = "manuell" if run_id is None else f"run{run_id}"
    name = f"backup-{slugify(calendar_name)}-{stamp}-{suffix}.zip"
    if os.path.exists(os.path.join(BACKUP_DIR, name)):
        raise RuntimeError("Es wurde gerade erst eine Sicherung erstellt – bitte kurz warten")
    path = os.path.join(BACKUP_DIR, name)
    tmp = path + ".tmp"

    combined = Calendar()
    combined.add("prodid", "-//CalShadow Backup//DE")
    combined.add("version", "2.0")
    seen_tz, seen_names = set(), set()
    count = 0

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for obj in objects:
            data = _object_data(obj)
            fname = os.path.basename(unquote(urlparse(str(obj.url)).path.rstrip("/"))) or f"obj{count}.ics"
            fname = re.sub(r"[^A-Za-z0-9._@-]", "_", fname)
            while fname in seen_names:
                fname = "_" + fname
            seen_names.add(fname)
            z.writestr(f"objects/{fname}", data)
            count += 1
            try:
                for comp in Calendar.from_ical(data).subcomponents:
                    if comp.name == "VTIMEZONE":
                        tzid = str(comp.get("TZID"))
                        if tzid in seen_tz:
                            continue
                        seen_tz.add(tzid)
                    combined.add_component(comp)
            except Exception:
                pass  # Rohdaten liegen trotzdem in objects/
        z.writestr("kalender.ics", combined.to_ical())
        download_seconds = time.monotonic() - t0
        z.writestr("meta.json", json.dumps({
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": run_id, "side": side, "calendar_name": calendar_name,
            "calendars": [str(c.url) for c in cals], "objects": count,
            # Gesamtdauer ohne das Schließen der ZIP-Datei, Download-Anteil separat
            "duration_seconds": round(time.monotonic() - t0, 2),
            "download_seconds": round(download_seconds, 2),
        }, indent=2))

    os.replace(tmp, path)
    return {"name": name, "objects": count, "size": os.path.getsize(path),
            "duration": time.monotonic() - t0, "download": download_seconds}


def list_backups() -> list[dict]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    items = []
    for name in os.listdir(BACKUP_DIR):
        m = NAME_RE.fullmatch(name)
        if not m:
            continue
        path = os.path.join(BACKUP_DIR, name)
        meta = {}
        try:
            with zipfile.ZipFile(path) as z:
                meta = json.loads(z.read("meta.json"))
        except Exception:
            pass
        items.append({
            "name": name,
            "created": datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc),
            "run_id": int(m.group(2)) if m.group(2) else None,
            "manual": m.group(2) is None,
            "side": meta.get("side", "target"),
            "objects": meta.get("objects"),
            "calendar_name": meta.get("calendar_name"),
            "duration": meta.get("duration_seconds"),
            "size": os.path.getsize(path),
        })
    items.sort(key=lambda b: (b["created"], b["run_id"] or 0), reverse=True)
    return items


def automatic_backups() -> list[dict]:
    """Nur die automatischen Sicherungen – nur für sie gelten Aufbewahrung und Mindestabstand."""
    return [b for b in list_backups() if not b["manual"]]


def retention_reasons(items: list[dict], s: dict, now: datetime | None = None) -> dict[str, str]:
    """Ordnet jeder aufzubewahrenden Sicherung den Grund zu (aktuell / stündlich / täglich).

    - die neuesten `backup_keep_last` Sicherungen
    - je die neueste Sicherung pro Stunde für die letzten `backup_keep_hourly` Stunden;
      mit `backup_hourly_skip_weekend` zählen Sa/So-Stunden nicht mit (und werden nicht stündlich behalten)
    - je die neueste Sicherung pro Tag für die letzten `backup_keep_daily` Tage
    `items` muss nach Zeit absteigend sortiert sein (wie von list_backups()).
    """
    tz = ZoneInfo(s["timezone"])
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    reasons: dict[str, str] = {}

    for b in items[:int(s["backup_keep_last"])]:
        reasons[b["name"]] = "aktuell"

    # Stunden-Buckets rückwärts ab der aktuellen Stunde sammeln
    hours, h, n = set(), now.replace(minute=0, second=0, microsecond=0), int(s["backup_keep_hourly"])
    while len(hours) < n:
        if not (s["backup_hourly_skip_weekend"] and h.weekday() >= 5):
            hours.add((h.date(), h.hour))
        h -= timedelta(hours=1)

    days = {now.date() - timedelta(days=i) for i in range(int(s["backup_keep_daily"]))}

    seen_hours, seen_days = set(), set()
    for b in items:  # neueste zuerst -> pro Bucket bleibt die neueste
        local = b["created"].astimezone(tz)
        hk, dk = (local.date(), local.hour), local.date()
        if hk in hours and hk not in seen_hours:
            seen_hours.add(hk)
            reasons.setdefault(b["name"], "stündlich")
        if dk in days and dk not in seen_days:
            seen_days.add(dk)
            reasons.setdefault(b["name"], "täglich")
    return reasons


def prune_backups(s: dict) -> list[str]:
    items = automatic_backups()
    keep = retention_reasons(items, s)
    removed = []
    for b in items:
        if b["name"] not in keep:
            os.remove(os.path.join(BACKUP_DIR, b["name"]))
            removed.append(b["name"])
    return removed


def last_automatic_backup_time() -> datetime | None:
    """Zeitpunkt der jüngsten automatischen Sicherung – nur aus den Dateinamen, ohne die ZIPs zu öffnen."""
    if not os.path.isdir(BACKUP_DIR):
        return None
    times = [datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
             for m in map(NAME_RE.fullmatch, os.listdir(BACKUP_DIR)) if m and m.group(2) is not None]
    return max(times, default=None)


def minutes_since_last_backup() -> float | None:
    items = automatic_backups()
    if not items:
        return None
    return (datetime.now(timezone.utc) - items[0]["created"]).total_seconds() / 60


def delete_backup(name: str) -> bool:
    """Löscht eine Sicherung (manuell oder automatisch); nur gültige Sicherungs-Dateinamen."""
    path = backup_path(name)
    if not path:
        return False
    os.remove(path)
    return True


def backup_path(name: str) -> str | None:
    if not NAME_RE.fullmatch(name):
        return None
    path = os.path.join(BACKUP_DIR, name)
    return path if os.path.isfile(path) else None
