"""Sync-Logik: private Termine lesen, filtern und als anonyme Blocker in den beruflichen Kalender schreiben."""

import hashlib
import hmac
import re
import threading
import time as _time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import caldav
import recurring_ical_events
import requests
from icalendar import Calendar, Event
from icalendar.prop import vDatetime, vDuration

from . import backup, db, notify

UID_PREFIX = "calshadow-"
MARKER_PROP = "X-CALSHADOW"
WEEKDAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

_sync_lock = threading.Lock()


@dataclass
class Blocker:
    uid: str
    start: datetime  # aware, UTC
    end: datetime    # aware, UTC
    title: str = ""  # Titel des privaten Termins – nur für Logs (Option), wird nie in den beruflichen Kalender geschrieben


class RunLogger:
    def __init__(self, run_id: int, echo: bool = True):
        self.run_id = run_id
        # echo=False (Termintitel-Option aktiv): nur Fehler in die Docker-Logs, da sich diese
        # nicht über die Weboberfläche löschen lassen; alles andere nur in die eigene Log-Datenbank
        self.echo = echo

    def _log(self, level: str, msg: str):
        if self.echo or level == "ERROR":
            print(f"[run {self.run_id}] {level:7} {msg}", flush=True)
        db.add_log(self.run_id, level, msg)

    def debug(self, msg): self._log("DEBUG", msg)
    def info(self, msg): self._log("INFO", msg)
    def success(self, msg): self._log("SUCCESS", msg)
    def warning(self, msg): self._log("WARNING", msg)
    def error(self, msg): self._log("ERROR", msg)


# ---------- Hilfsfunktionen ----------

def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _client(url: str, user: str, password: str) -> caldav.DAVClient:
    return caldav.DAVClient(url=url, username=user or None, password=password or None, timeout=60)


# ---------- Nur-Lese-Zugriff auf den privaten Kalender ----------

# HTTP-Methoden, die einen CalDAV-Server garantiert nicht verändern.
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND", "REPORT"})


class BlockedRequest(RuntimeError):
    pass


def _check_read_only(method) -> None:
    if str(method).upper() not in READ_ONLY_METHODS:
        raise BlockedRequest(
            f"Blockiert: schreibender Zugriff ({method}) auf den privaten Kalender ist nicht erlaubt"
        )


class ReadOnlyDAVClient(caldav.DAVClient):
    """DAV-Client, der jede verändernde Anfrage (PUT, DELETE, PROPPATCH, MKCALENDAR, …) verweigert.

    Doppelt abgesichert: in DAVClient.request() und direkt in der darunterliegenden
    requests-Session, damit auch interne Aufrufpfade der caldav-Bibliothek abgedeckt sind.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        session_request = self.session.request

        def guarded(method, url, *a, **kw):
            _check_read_only(method)
            return session_request(method, url, *a, **kw)

        self.session.request = guarded

    def request(self, url, method="GET", *args, **kwargs):
        _check_read_only(method)
        return super().request(url, method, *args, **kwargs)


def describe_error(e: Exception, calendar: str | None = None) -> str:
    """Verständliche Meldung für typische Verbindungsfehler; sonst die Original-Meldung."""
    where = f" am {calendar}" if calendar else ""
    if isinstance(e, caldav.lib.error.AuthorizationError):
        return (f"Anmeldung{where} abgelehnt (HTTP 401/403): Benutzername oder Passwort stimmen nicht, "
                "oder der Server verlangt ein App-Passwort")
    if isinstance(e, caldav.lib.error.NotFoundError):
        return f"Kalender-URL{where} nicht gefunden (HTTP 404) – Adresse prüfen"
    to = f" zum {calendar}" if calendar else ""
    if isinstance(e, requests.exceptions.Timeout):
        return f"Zeitüberschreitung bei der Verbindung{to} – der Server antwortet nicht"
    if isinstance(e, requests.exceptions.SSLError):
        return f"TLS-/Zertifikatsfehler bei der Verbindung{to}"
    if isinstance(e, requests.exceptions.ConnectionError):
        return f"Keine Verbindung{to} – Server nicht erreichbar, Adresse und Netzwerk prüfen"
    return str(e)


def _source_client(url: str, user: str, password: str) -> ReadOnlyDAVClient:
    return ReadOnlyDAVClient(url=url, username=user or None, password=password or None, timeout=60)


def _host(url: str) -> str:
    return (urlparse(url.strip()).hostname or "").lower()


def check_target_is_not_source(s: dict) -> None:
    """Verhindert, dass versehentlich ein privater (Quell-)Kalender als Schreibziel eingetragen wird."""
    target_hosts = {_host(u) for u in (s["target_url"], s["target_calendar"]) if u.strip()}
    source_hosts = {_host(s["source_url"])} | {
        _host(u) for u in s["source_calendars"].splitlines() if u.strip()
    }
    bad = {h for h in target_hosts if h and (h in source_hosts or h == "posteo.de" or h.endswith(".posteo.de"))}
    if bad:
        raise RuntimeError(
            f"Sicherheitsstopp: Der berufliche Kalender „{s['target_name']}“ liegt auf dem Server des "
            f"privaten Kalenders ({', '.join(sorted(bad))}). In den privaten Kalender wird niemals "
            "geschrieben – bitte den Server des beruflichen Kalenders eintragen."
        )


def _to_local(value, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)  # "floating" Zeit
    return value.astimezone(tz)


def _fmt(dt: datetime, tz: ZoneInfo) -> str:
    d = dt.astimezone(tz)
    return f"{WEEKDAY_NAMES[d.weekday()]} {d:%d.%m.%Y %H:%M}"


def _line(verb: str, when: str, detail: str = "", title: str = "") -> str:
    """Log-Zeile mit festem Präfix: Datum/Uhrzeit stehen in der Log-Ansicht (Festbreitenschrift) untereinander,
    Details und ggf. der Termintitel folgen dahinter."""
    text = f"{verb:<19}{when}"
    if detail:
        text += f" · {detail}"
    if title:
        text += f" „{title}“"
    return text


def _make_uid(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]
    return f"{UID_PREFIX}{digest}"


def list_calendars(url: str, user: str, password: str, read_only: bool) -> list[dict]:
    """Für den Verbindungstest: alle Kalender des Principals (oder die URL selbst)."""
    client = _source_client(url, user, password) if read_only else _client(url, user, password)
    try:
        cals = client.principal().calendars()
        return [{"name": c.get_display_name() or str(c.url), "url": str(c.url)} for c in cals]
    except Exception:
        cal = client.calendar(url=url)
        return [{"name": cal.get_display_name() or url, "url": url}]


# ---------- Quelle lesen ----------

def _fetch_raw(cal: caldav.Calendar, start: datetime, end: datetime, log: RunLogger) -> list:
    try:
        return cal.search(start=start, end=end, event=True, expand=False)
    except Exception as e:
        log.warning(f"Zeitbereichs-Suche fehlgeschlagen ({e}), lade alle Termine des Kalenders")
        return cal.events()


def collect_blockers(s: dict, log: RunLogger, stats: dict) -> tuple[dict[str, Blocker], datetime, datetime]:
    tz = ZoneInfo(s["timezone"])
    today = datetime.now(tz).date()
    win_start = datetime.combine(today - timedelta(days=int(s["days_back"])), time.min, tz)
    win_end = datetime.combine(today + timedelta(days=int(s["days_ahead"]) + 1), time.min, tz)
    work_start, work_end = _parse_hhmm(s["work_start"]), _parse_hhmm(s["work_end"])
    weekdays = set(int(d) for d in s["weekdays"])

    log.info(
        f"Zeitfenster: {win_start:%d.%m.%Y} – {(win_end - timedelta(days=1)):%d.%m.%Y}, "
        f"Arbeitszeit {s['work_start']}–{s['work_end']}, Modus "
        f"{'nur komplett innerhalb' if s['filter_mode'] == 'strict' else 'auf Arbeitszeit zuschneiden'}"
    )

    urls = [u.strip() for u in s["source_calendars"].splitlines() if u.strip()]
    if not urls:
        raise RuntimeError("Kein privater Kalender konfiguriert")

    client = _source_client(s["source_url"], s["source_username"], s["source_password"])
    blockers: dict[str, Blocker] = {}

    if s["source_mode"] == "freebusy":
        for url in urls:
            periods = _fetch_freebusy(client, url, win_start, win_end)
            log.info(f"Privater Kalender „{s['source_name']}“: {len(periods)} belegte Zeiträume geladen "
                     f"(nur Frei/Belegt, ohne Termininhalte) ({url})")
            for occ in periods:
                stats["source_events"] += 1
                for b in _occurrence_to_blockers(occ, set(), s, tz, work_start, work_end,
                                                 weekdays, win_start, win_end, log, stats):
                    blockers[b.uid] = b
        return blockers, win_start, win_end

    for url in urls:
        cal = client.calendar(url=url)
        raw = _fetch_raw(cal, win_start, win_end, log)
        log.info(f"Privater Kalender „{s['source_name']}“: {len(raw)} Kalenderobjekt(e) geladen ({url})")

        for obj in raw:
            try:
                ical = Calendar.from_ical(obj.data)
            except Exception as e:
                log.warning(f"Kalenderobjekt {obj.url} nicht lesbar: {e}")
                continue

            recurring_uids = {
                str(c.get("UID")) for c in ical.walk("VEVENT") if c.get("RRULE") or c.get("RDATE")
            }
            try:
                occurrences = recurring_ical_events.of(ical).between(win_start, win_end)
            except Exception as e:
                log.warning(f"Serie in {obj.url} konnte nicht expandiert werden: {e}")
                continue

            for occ in occurrences:
                stats["source_events"] += 1
                for b in _occurrence_to_blockers(occ, recurring_uids, s, tz, work_start, work_end,
                                                 weekdays, win_start, win_end, log, stats):
                    blockers[b.uid] = b

    return blockers, win_start, win_end


def _fetch_freebusy(client: ReadOnlyDAVClient, url: str, win_start: datetime, win_end: datetime) -> list[Event]:
    """Frei/Belegt-Abfrage (CalDAV free-busy-query, RFC 4791 7.10): der Server liefert nur belegte Zeiträume,
    keine Titel, Orte, Beschreibungen oder Teilnehmer. Als „frei“ markierte Termine lässt er selbst weg.

    Jeder Zeitraum wird als minimaler Pseudo-Termin zurückgegeben, damit dieselben Regeln greifen:
    Mitternacht bis Mitternacht (lokal oder UTC) gilt als ganztägig, BUSY-TENTATIVE als vorläufig, FREE als frei.
    """
    fmt = "%Y%m%dT%H%M%SZ"
    body = f'''<?xml version="1.0" encoding="utf-8"?>
<C:free-busy-query xmlns:C="urn:ietf:params:xml:ns:caldav">
  <C:time-range start="{win_start.astimezone(timezone.utc):{fmt}}" end="{win_end.astimezone(timezone.utc):{fmt}}"/>
</C:free-busy-query>'''
    r = client.request(url, "REPORT", body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    text = _body(r)
    if r.status != 200 or "VFREEBUSY" not in text:
        raise RuntimeError(
            f"Der private Kalender unterstützt keine Frei/Belegt-Abfrage (HTTP {r.status}). "
            "In den Einstellungen die Abfrageart „Termine lesen“ wählen."
        )
    tz = win_start.tzinfo
    text = re.sub(r"\r?\n[ \t]", "", text)  # Zeilenumbrüche (Folding) auflösen

    # (Start-Text, Ende-Text, FBTYPE) sammeln – zwei Formate:
    # 1. RFC 4791/5545 (z. B. Posteo): ein VFREEBUSY mit FREEBUSY-Zeilen, DTSTART/DTEND = Abfragezeitraum
    # 2. z. B. Radicale: ein VFREEBUSY je belegtem Zeitraum, mit DTSTART/DTEND und FBTYPE als Eigenschaft
    raw_periods = []
    for block in re.findall(r"^BEGIN:VFREEBUSY\s*$(.*?)^END:VFREEBUSY\s*$", text, re.M | re.S):
        lines = re.findall(r"^FREEBUSY((?:;[^:\r\n]*)?):([^\r\n]+)", block, re.M)
        if lines:
            for params, values in lines:
                fbtype_match = re.search(r"FBTYPE=([A-Za-z-]+)", params)
                fbtype = fbtype_match.group(1).upper() if fbtype_match else "BUSY"
                for value in values.split(","):
                    start_s, _, end_s = value.strip().partition("/")
                    raw_periods.append((start_s, end_s, fbtype))
            continue
        fbtype_prop = re.search(r"^FBTYPE[^:\r\n]*:([A-Za-z-]+)", block, re.M)
        dtstart = re.search(r"^DTSTART[^:\r\n]*:([^\r\n]+)", block, re.M)
        dtend = re.search(r"^(DTEND|DURATION)[^:\r\n]*:([^\r\n]+)", block, re.M)
        if fbtype_prop and dtstart and dtend:
            raw_periods.append((dtstart.group(1).strip(), dtend.group(2).strip(), fbtype_prop.group(1).upper()))

    events = []
    def parse_dt(value):
        if re.fullmatch(r"\d{8}", value):  # reines Datum (ganztägig) -> Mitternacht UTC
            return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
        return vDatetime.from_ical(value)

    for start_s, end_s, fbtype in raw_periods:
        start = parse_dt(start_s)
        end = start + vDuration.from_ical(end_s) if end_s.upper().startswith(("P", "+P", "-P")) else parse_dt(end_s)
        if start.tzinfo is None:  # FREEBUSY-Zeiten sind laut RFC immer UTC
            start, end = start.replace(tzinfo=timezone.utc), end.replace(tzinfo=timezone.utc)
        ls, le = start.astimezone(tz), end.astimezone(tz)
        us, ue = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        ev = Event()
        ev.add("uid", f"freebusy|{start.isoformat()}|{end.isoformat()}")
        if ls.time() == time.min and le.time() == time.min and le.date() > ls.date():
            ev.add("dtstart", ls.date())
            ev.add("dtend", le.date())
        elif us.time() == time.min and ue.time() == time.min and ue.date() > us.date():
            # Ganztägige Termine sind zeitzonenlos; manche Server (z. B. Posteo) liefern sie als UTC-Mitternacht
            ev.add("dtstart", us.date())
            ev.add("dtend", ue.date())
        else:
            ev.add("dtstart", ls)
            ev.add("dtend", le)
        if fbtype == "FREE":
            ev.add("transp", "TRANSPARENT")
        elif fbtype == "BUSY-TENTATIVE":
            ev.add("status", "TENTATIVE")
        events.append(ev)
    return events


def _occurrence_to_blockers(occ, recurring_uids, s, tz, work_start, work_end, weekdays,
                            win_start, win_end, log, stats) -> list[Blocker]:
    uid = str(occ.get("UID", ""))
    status = str(occ.get("STATUS", "")).upper()
    transp = str(occ.get("TRANSP", "OPAQUE")).upper()
    dtstart = occ.get("DTSTART").dt
    dtend_prop = occ.get("DTEND")
    # Für Logs nur ein Kürzel der UID, damit keine Inhalte im Log landen
    ref = hashlib.sha256(uid.encode()).hexdigest()[:8]
    # Titel nur mit ausdrücklich aktivierter Debug-Option
    title = str(occ.get("SUMMARY", "")).strip() if s["log_event_titles"] else ""

    all_day = not isinstance(dtstart, datetime)
    if all_day:
        when = f"{WEEKDAY_NAMES[dtstart.weekday()]} {dtstart:%d.%m.%Y}, ganztägig"
    else:
        start = _to_local(dtstart, tz)
        if dtend_prop:
            end = _to_local(dtend_prop.dt, tz)
        elif occ.get("DURATION"):
            end = start + occ.get("DURATION").dt
        else:
            end = start
        end_l = end.astimezone(tz)
        when = f"{_fmt(start, tz)} – {end_l:%H:%M}" if end_l.date() == start.date() else f"{_fmt(start, tz)} – {_fmt(end, tz)}"

    def skip(reason: str):
        stats["skipped"] += 1
        log.debug(_line("Übersprungen", when, f"{reason} [{ref}]", title))
        return []

    if status == "CANCELLED":
        return skip("abgesagt")
    if s["skip_transparent"] and transp == "TRANSPARENT":
        return skip("als 'frei' markiert")
    if s["skip_tentative"] and status == "TENTATIVE":
        return skip("nur vorläufig")

    if all_day:
        if not s["include_all_day"]:
            return skip("ganztägige Termine sind ausgeschaltet")
        end_date = dtend_prop.dt if dtend_prop else dtstart + timedelta(days=1)
        if isinstance(end_date, datetime):
            end_date = end_date.date()
        segments = []
        d = dtstart
        while d < end_date:
            segments.append((datetime.combine(d, work_start, tz), datetime.combine(d, work_end, tz)))
            d += timedelta(days=1)
    else:
        if end <= start:
            return skip("keine Dauer")

        if s["filter_mode"] == "strict":
            inside = (start.date() == end.date()
                      and start.time() >= work_start and end.time() <= work_end)
            if not inside:
                return skip("außerhalb Arbeitszeit")
            segments = [(start, end)]
        else:
            segments = []
            d = start.date()
            while datetime.combine(d, time.min, tz) < end:
                ws, we = datetime.combine(d, work_start, tz), datetime.combine(d, work_end, tz)
                seg_s, seg_e = max(start, ws), min(end, we)
                if seg_s < seg_e:
                    segments.append((seg_s, seg_e))
                d += timedelta(days=1)
            if not segments:
                return skip("außerhalb Arbeitszeit")

    # Schlüssel für stabile UIDs: Einzeltermine nur über UID (Zeitänderung -> Update),
    # Serienvorkommen zusätzlich über RECURRENCE-ID bzw. Startzeit.
    rid = occ.get("RECURRENCE-ID")
    if rid is not None:
        occ_key = rid.dt.isoformat()
    elif uid in recurring_uids:
        occ_key = dtstart.isoformat()
    else:
        occ_key = ""

    result = []
    for seg_s, seg_e in segments:
        if seg_s.weekday() not in weekdays:
            skip(f"Wochentag {WEEKDAY_NAMES[seg_s.weekday()]} nicht aktiv")
            continue
        if seg_e <= win_start or seg_s >= win_end:
            continue
        day_key = seg_s.date().isoformat() if len(segments) > 1 else ""
        result.append(Blocker(
            uid=_make_uid(uid, occ_key, day_key),
            start=seg_s.astimezone(timezone.utc),
            end=seg_e.astimezone(timezone.utc),
            title=title,
        ))
    return result


# ---------- Ziel schreiben (beruflicher Kalender) ----------
#
# Ein Termin im beruflichen Kalender gilt nur dann als eigener Blocker, wenn ALLE Bedingungen erfüllt sind:
#   1. UID hat exakt das Format calshadow-<40 Hex-Zeichen>
#   2. X-CALSHADOW enthält eine gültige HMAC-SHA256-Signatur dieser UID mit dem geheimen
#      Schlüssel dieser Installation (fälschungssicher, nicht mit anderen Tools/Kopien verwechselbar)
#   3. das Kalenderobjekt enthält genau einen VEVENT (keine Serie, keine Ausnahmen)
#   4. keine Teilnehmer/Organisator (also keine Einladung), feste Start- und Endzeit
# Der Titel spielt für die Erkennung keine Rolle.
#
# Zusätzlich lässt der Ziel-Client auf HTTP-Ebene nur PUT/DELETE auf einzelne, vorher explizit
# freigegebene Objekt-URLs innerhalb des Zielkalenders zu. Vor jedem Ändern/Löschen wird das Objekt
# frisch geladen, erneut geprüft und nur mit passendem ETag (If-Match) geschrieben.

UID_RE = re.compile(r"calshadow-[0-9a-f]{40}")


def _signature(uid: str, secret: str) -> str:
    return hmac.new(secret.encode(), uid.encode(), hashlib.sha256).hexdigest()


def _norm_url(url) -> tuple:
    p = urlparse(str(url))
    port = p.port or (443 if p.scheme == "https" else 80)
    return (p.scheme.lower(), (p.hostname or "").lower(), port, unquote(p.path).rstrip("/"))


class TargetDAVClient(caldav.DAVClient):
    """DAV-Client für den beruflichen Kalender: lesen ja, schreiben nur auf explizit freigegebene Blocker-URLs."""

    def __init__(self, calendar_url: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._collection = _norm_url(calendar_url)
        self._writable: set[tuple] = set()
        session_request = self.session.request

        def guarded(method, url, *a, **kw):
            self._check(method, url)
            return session_request(method, url, *a, **kw)

        self.session.request = guarded

    def allow_write(self, url) -> None:
        n = _norm_url(url)
        parent = n[:3] + (n[3].rsplit("/", 1)[0],)
        if parent != self._collection:
            raise BlockedRequest(f"Blockiert: {url} liegt nicht direkt im Zielkalender")
        self._writable.add(n)

    def _check(self, method, url) -> None:
        m = str(method).upper()
        if m in READ_ONLY_METHODS:
            return
        if m in ("PUT", "DELETE") and _norm_url(url) in self._writable:
            return
        raise BlockedRequest(f"Blockiert: {m} auf {url} – kein freigegebener eigener Blocker")

    def request(self, url, method="GET", *args, **kwargs):
        self._check(method, url)
        return super().request(url, method, *args, **kwargs)


def _blocker_ical(b: Blocker, s: dict) -> str:
    cal = Calendar()
    cal.add("prodid", "-//CalShadow//DE")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", b.uid)
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("dtstart", b.start)
    ev.add("dtend", b.end)
    ev.add("summary", s["blocker_title"])
    if s["blocker_description"].strip():
        ev.add("description", s["blocker_description"].strip())
    ev.add("class", s["blocker_class"])
    ev.add("transp", "OPAQUE")
    ev.add(MARKER_PROP, _signature(b.uid, s["instance_secret"]))
    cal.add_component(ev)
    return cal.to_ical().decode()


def parse_own_blocker(data, secret: str) -> tuple[dict | None, str | None]:
    """Gibt (Infos, None) für eigene Blocker zurück, (None, Grund) für Doppelgänger,
    (None, None) für normale fremde Termine."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    try:
        ical = Calendar.from_ical(data)
    except Exception:
        return None, None
    comps = [c for c in ical.subcomponents if c.name != "VTIMEZONE"]
    looks_like = any(
        str(c.get("UID", "")).startswith(UID_PREFIX) or c.get(MARKER_PROP) is not None for c in comps
    )
    if not looks_like:
        return None, None
    if len(comps) != 1 or comps[0].name != "VEVENT":
        return None, "mehrere Komponenten"
    ev = comps[0]
    uid = str(ev.get("UID", ""))
    if not UID_RE.fullmatch(uid):
        return None, "UID-Format ungültig"
    if not hmac.compare_digest(str(ev.get(MARKER_PROP, "")), _signature(uid, secret)):
        return None, "Signatur ungültig (nicht von dieser Installation erstellt)"
    for prop in ("RRULE", "RDATE", "RECURRENCE-ID", "ATTENDEE", "ORGANIZER"):
        if ev.get(prop) is not None:
            return None, f"enthält {prop}"
    dtstart, dtend = ev.get("DTSTART"), ev.get("DTEND")
    if dtstart is None or dtend is None or not isinstance(dtstart.dt, datetime):
        return None, "keine feste Start-/Endzeit"
    return {
        "uid": uid,
        "start": _to_local(dtstart.dt, timezone.utc),
        "end": _to_local(dtend.dt, timezone.utc),
        "summary": str(ev.get("SUMMARY", "")),
        "description": str(ev.get("DESCRIPTION", "")),
        "class": str(ev.get("CLASS", "")),
    }, None


class UidFilterUnsupported(RuntimeError):
    pass


def _fetch_uid_filtered(tclient: TargetDAVClient, cal: caldav.Calendar, win_start: datetime | None,
                        win_end: datetime | None) -> list[tuple[str, str]]:
    """Fragt beim Server nur Termine ab, deren UID „calshadow-“ enthält (CalDAV prop-filter/text-match).

    So verlassen fremde Termine (z. B. von Kollegen) den Server gar nicht erst.
    Gibt (URL, iCalendar-Daten) zurück. Ohne Zeitraum (None) werden alle passenden Termine geliefert.
    """
    fmt = "%Y%m%dT%H%M%SZ"
    time_range = (f'<C:time-range start="{win_start.astimezone(timezone.utc):{fmt}}" '
                  f'end="{win_end.astimezone(timezone.utc):{fmt}}"/>' if win_start and win_end else "")
    body = f'''<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop><D:getetag/><C:calendar-data/></D:prop>
  <C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">
    {time_range}
    <C:prop-filter name="UID"><C:text-match collation="i;octet">{UID_PREFIX}</C:text-match></C:prop-filter>
  </C:comp-filter></C:comp-filter></C:filter>
</C:calendar-query>'''
    r = tclient.request(str(cal.url), "REPORT", body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
    if r.status != 207:
        raise UidFilterUnsupported(
            f"Der berufliche Kalender unterstützt den serverseitigen UID-Filter nicht (HTTP {r.status}). "
            "In den Einstellungen „Nur eigene Blocker abfragen“ deaktivieren, um alle Termine zu lesen."
        )
    raw = getattr(r, "_raw", None) or r.raw
    root = ET.fromstring(raw if isinstance(raw, bytes) else str(raw).encode())
    result = []
    for resp in root.iter("{DAV:}response"):
        href = resp.findtext("{DAV:}href")
        data = None
        for el in resp.iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
            data = el.text
        if href and data:
            result.append((str(cal.url.join(href)), data))
    return result


def _managed_target_events(tclient: TargetDAVClient, cal: caldav.Calendar, win_start: datetime,
                           win_end: datetime, secret: str, log: RunLogger,
                           uid_filter: bool = True) -> tuple[dict, list]:
    found, duplicates = {}, []
    if uid_filter:
        candidates = _fetch_uid_filtered(tclient, cal, win_start, win_end)
        foreign = sum(1 for _, data in candidates if UID_PREFIX not in data)
        if foreign:
            log.warning(f"Der Server hat den UID-Filter ignoriert und {foreign} fremde Termin(e) geliefert – "
                        "sie werden nicht angefasst, wurden aber gelesen")
    else:
        candidates = [(obj.url, obj.data) for obj in cal.search(start=win_start, end=win_end, event=True, expand=False)]
    for url, data in candidates:
        info, reason = parse_own_blocker(data, secret)
        if reason:
            log.warning(f"Nicht angefasst: {url} sieht aus wie ein Blocker, aber {reason}")
        if not info:
            continue
        info["url"] = url
        if info["uid"] in found:
            duplicates.append(info)
        else:
            found[info["uid"]] = info
    return found, duplicates


def _body(r) -> str:
    raw = getattr(r, "_raw", None) or r.raw
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def _put_new(tclient: TargetDAVClient, tcal: caldav.Calendar, b: Blocker, s: dict) -> bool:
    url = tcal.url.join(quote(b.uid) + ".ics")
    tclient.allow_write(url)
    r = tclient.request(str(url), "PUT", _blocker_ical(b, s),
                        {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
    if r.status == 412:
        return False  # an dieser URL existiert schon etwas -> nie überschreiben
    if r.status not in (200, 201, 204):
        raise RuntimeError(f"Schreiben in den beruflichen Kalender fehlgeschlagen (PUT): {r.status} {r.reason}")
    return True


def _modify_own(tclient: TargetDAVClient, url, secret: str, method: str, body: str = "") -> str | None:
    """Lädt das Objekt frisch, prüft erneut den Besitz und schreibt nur mit passendem ETag.
    Gibt None bei Erfolg oder einen Grund zurück, warum nichts geändert wurde."""
    r = tclient.request(str(url), "GET")
    if r.status != 200:
        return f"Objekt nicht abrufbar ({r.status})"
    info, reason = parse_own_blocker(_body(r), secret)
    if not info:
        return reason or "kein eigener Blocker"
    headers = {"Content-Type": "text/calendar; charset=utf-8"} if method == "PUT" else {}
    etag = r.headers.get("ETag")
    if etag:
        headers["If-Match"] = etag
    tclient.allow_write(url)
    r = tclient.request(str(url), method, body, headers)
    if r.status == 412:
        return "zwischenzeitlich verändert (ETag)"
    if r.status not in (200, 201, 204):
        raise RuntimeError(f"Schreiben in den beruflichen Kalender fehlgeschlagen ({method}): {r.status} {r.reason}")
    return None


# ---------- Test-Blocker (Einstellungen) ----------
#
# Fester, signierter Blocker am nächsten Montag 17:00–17:30. Er wird vom regulären Sync
# nie angefasst und lässt sich nur über die Einstellungen wieder löschen.

TEST_UID = _make_uid("calshadow-test-blocker")


def _target(s: dict) -> tuple[TargetDAVClient, caldav.Calendar]:
    if not s["target_calendar"]:
        raise RuntimeError("Bitte zuerst die Zielkalender-URL eintragen")
    check_target_is_not_source(s)
    tclient = TargetDAVClient(
        s["target_calendar"], url=s["target_url"] or s["target_calendar"],
        username=s["target_username"] or None, password=s["target_password"] or None, timeout=60,
    )
    return tclient, tclient.calendar(url=s["target_calendar"])


def _test_url(tcal: caldav.Calendar):
    return tcal.url.join(quote(TEST_UID) + ".ics")


def _next_monday_slot(tz: ZoneInfo) -> Blocker:
    now = datetime.now(tz)
    day = now.date() + timedelta(days=(7 - now.weekday()) % 7)
    if datetime.combine(day, time(17, 0), tz) <= now:
        day += timedelta(days=7)
    return Blocker(
        uid=TEST_UID,
        start=datetime.combine(day, time(17, 0), tz).astimezone(timezone.utc),
        end=datetime.combine(day, time(17, 30), tz).astimezone(timezone.utc),
    )


def manual_backup(side: str) -> dict:
    """Manuelle Sicherung des beruflichen ("target") oder privaten ("source") Kalenders.

    Wird bewusst nicht an Uptime Kuma gemeldet – der Push-Monitor überwacht nur die automatischen Sicherungen.
    """
    if not _sync_lock.acquire(blocking=False):
        raise RuntimeError("Es läuft gerade ein Sync – bitte kurz warten")
    try:
        s = db.get_settings()
        name = s["target_name"] if side == "target" else s["source_name"]
        label = f"{'beruflichen' if side == 'target' else 'privaten'} Kalender „{name}“"

        try:
            if side == "target":
                _, tcal = _target(s)
                cals = [tcal]
            else:
                urls = [u.strip() for u in s["source_calendars"].splitlines() if u.strip()]
                if not urls:
                    raise RuntimeError("Kein privater Kalender konfiguriert")
                client = _source_client(s["source_url"], s["source_username"], s["source_password"])
                cals = [client.calendar(url=u) for u in urls]
            info = backup.create_backup(cals, None, name, side)
        except Exception as e:
            msg = describe_error(e, label)
            db.add_log(None, "ERROR", f"Manuelle Sicherung fehlgeschlagen: {msg}")
            raise RuntimeError(msg)
        db.add_log(None, "SUCCESS", f"Manuelle Sicherung vom {label} erstellt: {info['name']} "
                                    f"({info['objects']} Objekte, {info['size'] / 1024:.0f} KB, "
                                    f"{info['duration']:.1f} s)")
        return info
    finally:
        _sync_lock.release()


def _all_own_blockers(tclient: TargetDAVClient, tcal: caldav.Calendar, s: dict) -> list[dict]:
    """Alle eigenen Blocker im beruflichen Kalender, unabhängig vom Zeitraum (inkl. Test-Blocker)."""
    if s["target_uid_filter"]:
        candidates = _fetch_uid_filtered(tclient, tcal, None, None)
    else:
        candidates = [(obj.url, obj.data) for obj in tcal.search(event=True, expand=False)]
    own = []
    for url, data in candidates:
        info, _ = parse_own_blocker(data, s["instance_secret"])
        if info:
            info["url"] = url
            own.append(info)
    return sorted(own, key=lambda i: i["start"])


def cleanup_blockers(execute: bool, disable_sync: bool = False) -> dict:
    """Entfernt alle eigenen Blocker aus dem beruflichen Kalender (execute=False: nur zählen).

    Vor dem Löschen wird der berufliche Kalender gesichert (manuelle Sicherung, bleibt erhalten);
    jeder Blocker wird vor dem Löschen frisch geladen und erneut per Signatur geprüft.
    """
    if not _sync_lock.acquire(blocking=False):
        raise RuntimeError("Es läuft gerade ein Sync oder eine Sicherung – bitte kurz warten")
    try:
        s = db.get_settings()
        tz = ZoneInfo(s["timezone"])
        label = f"beruflichen Kalender „{s['target_name']}“"
        if not execute:
            try:
                tclient, tcal = _target(s)
                own = _all_own_blockers(tclient, tcal, s)
            except Exception as e:
                raise RuntimeError(describe_error(e, label))
            now = datetime.now(timezone.utc)
            fmt = lambda i: f"{_fmt(i['start'], tz)}"
            return {"count": len(own), "past": sum(1 for i in own if i["end"] < now),
                    "first": fmt(own[0]) if own else None, "last": fmt(own[-1]) if own else None,
                    "sync_enabled": bool(s["enabled"])}

        run_id = db.start_run("Aufräumen", False)
        log = RunLogger(run_id)
        stats = {"source_events": 0, "created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "skipped": 0}
        log.info(f"Aufräumen: alle Blocker aus dem {label} entfernen")
        try:
            if disable_sync and s["enabled"]:
                db.save_settings({"enabled": False})
                log.info("Automatischer Sync deaktiviert, damit die Blocker nicht sofort neu angelegt werden")
            tclient, tcal = _target(s)
            try:
                info = backup.create_backup([tcal], None, s["target_name"], "target")
            except Exception as e:
                raise RuntimeError(f"Sicherung vor dem Aufräumen fehlgeschlagen, nichts gelöscht: {describe_error(e, label)}")
            log.info(f"Sicherung erstellt: {info['name']} ({info['objects']} Objekte)")
            own = _all_own_blockers(tclient, tcal, s)
            log.info(f"{len(own)} eigene Blocker gefunden (per Signatur verifiziert)")
            for b in own:
                when = f"{_fmt(b['start'], tz)} – {b['end'].astimezone(tz):%H:%M}"
                reason = _modify_own(tclient, b["url"], s["instance_secret"], "DELETE")
                if reason:
                    stats["skipped"] += 1
                    log.warning(_line("Nicht gelöscht", when, reason))
                else:
                    stats["deleted"] += 1
                    log.success(_line("Gelöscht", when, "Aufräumen"))
            db.replace_blockers([])
            log.info(f"Fertig: {stats['deleted']} Blocker entfernt"
                     + (f", {stats['skipped']} nicht gelöscht (siehe Warnungen)" if stats["skipped"] else ""))
            db.finish_run(run_id, "success", stats)
        except Exception as e:
            msg = describe_error(e, label)
            log.error(f"Aufräumen fehlgeschlagen: {msg}")
            db.finish_run(run_id, "error", stats, msg)
            raise RuntimeError(msg)
        return {"deleted": stats["deleted"], "skipped": stats["skipped"], "run_id": run_id,
                "sync_disabled": bool(disable_sync and s["enabled"])}
    finally:
        _sync_lock.release()


def test_blocker_status(s: dict) -> dict:
    tclient, tcal = _target(s)
    r = tclient.request(str(_test_url(tcal)), "GET")
    if r.status != 200:
        return {"exists": False}
    info, _ = parse_own_blocker(_body(r), s["instance_secret"])
    if not info:
        return {"exists": False}
    tz = ZoneInfo(s["timezone"])
    return {"exists": True, "label": f"{_fmt(info['start'], tz)} – {info['end'].astimezone(tz):%H:%M}"}


def create_test_blocker(s: dict) -> dict:
    tclient, tcal = _target(s)
    b = _next_monday_slot(ZoneInfo(s["timezone"]))
    if not _put_new(tclient, tcal, b, {**s, "blocker_title": f"{s['blocker_title']} (Test)"}):
        raise RuntimeError("Es existiert bereits ein Test-Blocker")
    return test_blocker_status(s)


def delete_test_blocker(s: dict) -> dict:
    tclient, tcal = _target(s)
    reason = _modify_own(tclient, _test_url(tcal), s["instance_secret"], "DELETE")
    if reason:
        raise RuntimeError(f"Test-Blocker nicht gelöscht: {reason}")
    return test_blocker_status(s)


def run_sync(trigger: str = "schedule", dry_run: bool = False) -> int | None:
    """Führt einen Sync-Lauf aus. Gibt die Run-ID zurück oder None, wenn bereits ein Lauf läuft."""
    if not _sync_lock.acquire(blocking=False):
        return None
    try:
        return _run(trigger, dry_run)
    finally:
        _sync_lock.release()


def is_running() -> bool:
    return _sync_lock.locked()


def _run(trigger: str, dry_run: bool) -> int:
    s = db.get_settings()
    run_id = db.start_run(trigger, dry_run)
    log = RunLogger(run_id, echo=not s["log_event_titles"])
    stats = {"source_events": 0, "created": 0, "updated": 0, "deleted": 0, "unchanged": 0, "skipped": 0}
    tz = ZoneInfo(s["timezone"])
    t0 = _time.monotonic()
    phase = "target"  # für verständliche Fehlermeldungen: welcher Kalender wird gerade angesprochen

    def push(kind: str, up: bool, msg: str, ping_ms: float | None = None):
        url = s[f"kuma_{kind}_push_url"]
        if not url.strip():
            return
        err = notify.kuma_push(url, up, msg, ping_ms)
        name = {"backup": "Sicherung", "sync": "Sync"}[kind]
        if err:
            log.warning(f"Uptime-Kuma-Push ({name}) fehlgeschlagen: {err}")
        else:
            log.debug(f"Uptime-Kuma-Push ({name}): {'up' if up else 'down'} – {msg}")

    log.info(f"Sync „{s['source_name']}“ → „{s['target_name']}“ gestartet ({'Dry-Run, keine Änderungen' if dry_run else 'live'}, Auslöser: {trigger})")
    try:
        if not s["target_calendar"]:
            raise RuntimeError("Kein beruflicher Kalender konfiguriert")
        check_target_is_not_source(s)

        tclient = TargetDAVClient(
            s["target_calendar"], url=s["target_url"] or s["target_calendar"],
            username=s["target_username"] or None, password=s["target_password"] or None, timeout=60,
        )
        tcal = tclient.calendar(url=s["target_calendar"])

        if s["backup_enabled"] and not dry_run:
            age = backup.minutes_since_last_backup()
            min_interval = int(s["backup_min_interval_minutes"])
            if age is not None and age < min_interval:
                log.info(f"Keine neue Sicherung: letzte ist {age:.0f} min alt (Mindestabstand {min_interval} min)")
            else:
                # Ohne erfolgreiche Sicherung wird nichts geschrieben (Exception bricht den Lauf ab).
                try:
                    info = backup.create_backup([tcal], run_id, s["target_name"], "target")
                except Exception as e:
                    reason = describe_error(e, f"beruflichen Kalender „{s['target_name']}“")
                    push("backup", False, f"Sicherung „{s['target_name']}“ fehlgeschlagen: {reason}")
                    raise RuntimeError(f"Sicherung fehlgeschlagen, Sync abgebrochen: {reason}")
                log.info(f"Sicherung „{s['target_name']}“ erstellt: {info['name']} ({info['objects']} Objekte, "
                         f"{info['size'] / 1024:.0f} KB, Dauer {info['duration']:.1f} s, "
                         f"davon Laden {info['download']:.1f} s)")
                push("backup", True, f"„{s['target_name']}“: {info['objects']} Objekte, {info['size'] / 1024:.0f} KB, "
                     f"{info['duration']:.1f} s", info["duration"] * 1000)
                removed = backup.prune_backups(s)
                if removed:
                    log.debug(f"Alte Sicherungen entfernt: {', '.join(removed)}")

        phase = "source"
        desired, win_start, win_end = collect_blockers(s, log, stats)
        phase = "target"
        log.info(f"{stats['source_events']} private Termin(e) im Zeitfenster, "
                 f"{len(desired)} Blocker erforderlich, {stats['skipped']} übersprungen")
        secret = s["instance_secret"]
        existing, duplicates = _managed_target_events(tclient, tcal, win_start, win_end, secret, log,
                                                      uid_filter=s["target_uid_filter"])
        existing.pop(TEST_UID, None)  # Test-Blocker gehört nicht zum Sync
        duplicates = [d for d in duplicates if d["uid"] != TEST_UID]
        log.info(f"Beruflicher Kalender „{s['target_name']}“: {len(existing)} eigene Blocker im Zeitfenster "
                 "gefunden (per Signatur verifiziert, "
                 f"{'nur eigene abgefragt' if s['target_uid_filter'] else 'alle Termine gelesen'})")

        to_delete = [existing[uid] for uid in existing if uid not in desired] + duplicates
        max_del = int(s["max_deletions"] or 0)
        if max_del and len(to_delete) > max_del:
            raise RuntimeError(
                f"Sicherheitsstopp: {len(to_delete)} Löschungen geplant, erlaubt sind {max_del}. "
                "Limit in den Einstellungen anpassen, falls das gewollt ist."
            )

        for uid, b in sorted(desired.items(), key=lambda kv: kv[1].start):
            label = f"{_fmt(b.start, tz)} – {b.end.astimezone(tz):%H:%M}"
            ex = existing.get(uid)
            if ex is None:
                if not dry_run and not _put_new(tclient, tcal, b, s):
                    log.warning(_line("Nicht angelegt", label, "Objekt-URL existiert bereits", b.title))
                    continue
                stats["created"] += 1
                log.success(_line("Neu", label, "", b.title))
            elif (ex["start"] != b.start or ex["end"] != b.end
                  or ex["summary"] != s["blocker_title"] or ex["class"] != s["blocker_class"]
                  or ex["description"] != s["blocker_description"].strip()):
                if not dry_run:
                    reason = _modify_own(tclient, ex["url"], secret, "PUT", _blocker_ical(b, s))
                    if reason:
                        log.warning(_line("Nicht aktualisiert", label, reason, b.title))
                        continue
                stats["updated"] += 1
                log.success(_line("Aktualisiert", label, "", b.title))
            else:
                stats["unchanged"] += 1

        for ex in to_delete:
            label = f"{_fmt(ex['start'], tz)} – {ex['end'].astimezone(tz):%H:%M}"
            if not dry_run:
                reason = _modify_own(tclient, ex["url"], secret, "DELETE")
                if reason:
                    log.warning(_line("Nicht gelöscht", label, reason))
                    continue
            stats["deleted"] += 1
            log.success(_line("Gelöscht", label, "privater Termin entfernt/verschoben"))

        if not dry_run:
            db.replace_blockers([(u, b.start.isoformat(), b.end.isoformat()) for u, b in desired.items()])

        log.info(
            f"Fertig: {stats['created']} neu, {stats['updated']} aktualisiert, "
            f"{stats['deleted']} gelöscht, {stats['unchanged']} unverändert"
        )
        db.finish_run(run_id, "success", stats)
        if not dry_run:
            push("sync", True, f"+{stats['created']} ~{stats['updated']} -{stats['deleted']} "
                 f"={stats['unchanged']}", (_time.monotonic() - t0) * 1000)
    except Exception as e:
        cal_label = (f"privaten Kalender „{s['source_name']}“" if phase == "source"
                     else f"beruflichen Kalender „{s['target_name']}“")
        msg = describe_error(e, cal_label)
        log.error(f"Sync fehlgeschlagen: {msg}")
        log.debug(traceback.format_exc())
        db.finish_run(run_id, "error", stats, msg)
        if not dry_run:
            push("sync", False, f"Sync fehlgeschlagen: {msg}")
    finally:
        try:
            db.prune(int(s["log_retention_days"]))
        except Exception:
            pass
    return run_id
