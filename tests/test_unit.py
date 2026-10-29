"""Unit-Tests ohne CalDAV-Server."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import caldav
import pytest
import requests
from icalendar import Event

from app import backup, db, sync

TZ = ZoneInfo("Europe/Berlin")
WIN_START = datetime(2026, 11, 2, tzinfo=TZ)
WIN_END = datetime(2026, 12, 31, tzinfo=TZ)


class QuietLog:
    def __init__(self):
        self.lines = []

    def _add(self, msg):
        self.lines.append(msg)

    debug = info = success = warning = error = _add


def settings(**overrides) -> dict:
    s = dict(db.DEFAULT_SETTINGS, instance_secret="geheim", timezone="Europe/Berlin")
    s.update(overrides)
    return s


def blockers_for(event: Event, **overrides) -> tuple[list, QuietLog]:
    s = settings(**overrides)
    log = QuietLog()
    stats = {"skipped": 0, "source_events": 0}
    result = sync._occurrence_to_blockers(
        event, set(), s, TZ, sync._parse_hhmm(s["work_start"]), sync._parse_hhmm(s["work_end"]),
        set(s["weekdays"]), WIN_START, WIN_END, log, stats,
    )
    return result, log


def event(start, end, **props) -> Event:
    ev = Event()
    ev.add("uid", "test-1")
    ev.add("dtstart", start)
    ev.add("dtend", end)
    for key, value in props.items():
        ev.add(key, value)
    return ev


def local(*args) -> datetime:
    return datetime(*args, tzinfo=TZ)


# ---------- Erkennung eigener Blocker ----------

def own_blocker_ics(uid: str, secret: str = "geheim", extra: str = "") -> str:
    b = sync.Blocker(uid, datetime(2026, 11, 2, 9, tzinfo=timezone.utc), datetime(2026, 11, 2, 10, tzinfo=timezone.utc))
    ics = sync._blocker_ical(b, settings(instance_secret=secret))
    return ics.replace("END:VEVENT", extra + "END:VEVENT") if extra else ics


def test_own_blocker_is_recognized():
    info, reason = sync.parse_own_blocker(own_blocker_ics("calshadow-" + "a" * 40), "geheim")
    assert reason is None and info["uid"] == "calshadow-" + "a" * 40


def test_blocker_with_wrong_signature_is_rejected():
    info, reason = sync.parse_own_blocker(own_blocker_ics("calshadow-" + "a" * 40, secret="andere"), "geheim")
    assert info is None and "Signatur" in reason


def test_blocker_with_attendee_is_rejected():
    ics = own_blocker_ics("calshadow-" + "a" * 40, extra="ATTENDEE:mailto:chef@example.org\r\n")
    info, reason = sync.parse_own_blocker(ics, "geheim")
    assert info is None and "ATTENDEE" in reason


def test_foreign_event_is_ignored_silently():
    ics = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:kollege\r\nDTSTART:20261102T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    assert sync.parse_own_blocker(ics, "geheim") == (None, None)


def test_title_is_irrelevant_for_recognition():
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:kollege\r\nSUMMARY:Privater Blocker\r\n"
           "DTSTART:20261102T090000Z\r\nDTEND:20261102T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert sync.parse_own_blocker(ics, "geheim") == (None, None)


def test_blocker_contains_no_private_content():
    s = settings(blocker_description="Fester Text")
    ics = sync._blocker_ical(sync.Blocker("calshadow-" + "b" * 40, local(2026, 11, 2, 9), local(2026, 11, 2, 10), title="Zahnarzt"), s)
    assert "Zahnarzt" not in ics and "SUMMARY:Privater Blocker" in ics and "DESCRIPTION:Fester Text" in ics


# ---------- Filterregeln ----------

def test_strict_mode_skips_events_outside_work_hours():
    inside, _ = blockers_for(event(local(2026, 11, 2, 10), local(2026, 11, 2, 11)))
    late, log = blockers_for(event(local(2026, 11, 2, 17), local(2026, 11, 2, 19)))
    assert len(inside) == 1 and late == [] and "außerhalb Arbeitszeit" in log.lines[0]


def test_clip_mode_cuts_to_work_hours():
    result, _ = blockers_for(event(local(2026, 11, 2, 17), local(2026, 11, 2, 19)), filter_mode="clip")
    assert [(b.start.astimezone(TZ).hour, b.end.astimezone(TZ).hour) for b in result] == [(17, 18)]


def test_transparent_tentative_and_all_day_rules():
    assert blockers_for(event(local(2026, 11, 2, 10), local(2026, 11, 2, 11), transp="TRANSPARENT"))[0] == []
    tentative = event(local(2026, 11, 2, 10), local(2026, 11, 2, 11), status="TENTATIVE")
    assert len(blockers_for(tentative)[0]) == 1
    assert blockers_for(tentative, skip_tentative=True)[0] == []
    all_day = event(date(2026, 11, 4), date(2026, 11, 5))
    assert blockers_for(all_day)[0] == []
    assert len(blockers_for(all_day, include_all_day=True)[0]) == 1


def test_inactive_weekday_is_skipped():
    saturday = event(local(2026, 11, 7, 10), local(2026, 11, 7, 11))
    result, log = blockers_for(saturday, weekdays=[0, 1, 2, 3, 4])
    assert result == [] and "Wochentag Sa" in log.lines[0]


def test_skip_log_lines_show_date_and_time():
    _, log = blockers_for(event(local(2026, 11, 2, 10), local(2026, 11, 2, 11), transp="TRANSPARENT"))
    assert "Mo 02.11.2026 10:00 – 11:00" in log.lines[0]


# ---------- Frei/Belegt-Antworten ----------

class FakeResponse:
    def __init__(self, text, status=200):
        self.status, self.headers, self._raw = status, {}, text.encode()


class FakeClient:
    def __init__(self, text, status=200):
        self.response = FakeResponse(text, status)

    def request(self, *args, **kwargs):
        return self.response


def test_freebusy_rfc_format_like_posteo():
    text = ("BEGIN:VCALENDAR\r\nBEGIN:VFREEBUSY\r\nDTSTART:20261101T230000Z\r\nDTEND:20261231T230000Z\r\n"
            "FREEBUSY:20261102T090000Z/20261102T100000Z,20261102T130000Z/PT1H30M\r\n"
            "FREEBUSY;FBTYPE=BUSY-TENTATIVE:20261103T090000Z/20261103T100000Z\r\n"
            "FREEBUSY;FBTYPE=FREE:20261104T090000Z/20261104T100000Z\r\n"
            "FREEBUSY:20261105T000000Z/20261107T000000Z\r\n"     # ganztägig, als UTC-Mitternacht wie bei Posteo
            "FREEBUSY:20261109T170000Z/2026110\r\n 9T190000Z\r\n"  # umbrochene Zeile
            "END:VFREEBUSY\r\nEND:VCALENDAR\r\n")
    events = sync._fetch_freebusy(FakeClient(text), "x", WIN_START, WIN_END)
    assert len(events) == 6
    assert events[2].get("STATUS") == "TENTATIVE" and events[3].get("TRANSP") == "TRANSPARENT"
    assert events[4]["DTSTART"].dt == date(2026, 11, 5)  # als ganztägig erkannt


def test_freebusy_one_block_per_period_like_radicale():
    text = ("BEGIN:VCALENDAR\r\nBEGIN:VFREEBUSY\r\nDTSTART:20261102T090000Z\r\nDTEND:20261102T100000Z\r\n"
            "FBTYPE:BUSY\r\nEND:VFREEBUSY\r\nBEGIN:VFREEBUSY\r\nDTSTART;VALUE=DATE:20261104\r\n"
            "DTEND;VALUE=DATE:20261105\r\nFBTYPE:BUSY\r\nEND:VFREEBUSY\r\nEND:VCALENDAR\r\n")
    events = sync._fetch_freebusy(FakeClient(text), "x", WIN_START, WIN_END)
    assert len(events) == 2 and events[1]["DTSTART"].dt == date(2026, 11, 4)


def test_freebusy_unsupported_server_raises():
    with pytest.raises(RuntimeError, match="Frei/Belegt"):
        sync._fetch_freebusy(FakeClient("nope", status=501), "x", WIN_START, WIN_END)


# ---------- Schreibschutz ----------

@pytest.mark.parametrize("method", ["PUT", "DELETE", "PROPPATCH", "MKCALENDAR", "MKCOL", "POST", "MOVE", "put"])
def test_private_calendar_client_blocks_writes(method):
    client = sync._source_client("http://127.0.0.1:9/", "u", "p")
    with pytest.raises(sync.BlockedRequest):
        client.request("http://127.0.0.1:9/cal/x.ics", method)
    with pytest.raises(sync.BlockedRequest):
        client.session.request(method, "http://127.0.0.1:9/cal/x.ics")


def test_target_client_only_writes_released_urls():
    client = sync.TargetDAVClient("http://h/cal/", url="http://h/", username="u", password="p")
    with pytest.raises(sync.BlockedRequest):
        client.request("http://h/cal/fremd.ics", "DELETE")
    with pytest.raises(sync.BlockedRequest):
        client.allow_write("http://h/cal/")            # der Kalender selbst
    with pytest.raises(sync.BlockedRequest):
        client.allow_write("http://h/anderer/x.ics")   # anderer Kalender
    client.allow_write("http://h/cal/calshadow-x.ics")
    with pytest.raises(sync.BlockedRequest):
        client.request("http://h/cal/calshadow-x.ics", "PROPPATCH")


def test_target_on_source_host_is_refused():
    s = settings(source_url="https://posteo.de:8443/", source_calendars="https://posteo.de:8443/calendars/x/default/",
                 target_url="", target_calendar="https://posteo.de:8443/calendars/x/default/")
    with pytest.raises(RuntimeError, match="Sicherheitsstopp"):
        sync.check_target_is_not_source(s)


# ---------- Sonstiges ----------

def test_error_messages_are_readable():
    assert "Anmeldung am X abgelehnt" in sync.describe_error(caldav.lib.error.AuthorizationError("x"), "X")
    assert "Keine Verbindung zum X" in sync.describe_error(requests.exceptions.ConnectionError("x"), "X")


def test_backup_retention_rules():
    now = datetime(2026, 11, 2, 10, 20, tzinfo=TZ)  # Montag
    items, t = [], now
    while t > now - timedelta(days=40):
        items.append({"name": t.isoformat(), "created": t.astimezone(timezone.utc)})
        t -= timedelta(minutes=15)
    reasons = backup.retention_reasons(items, settings(), now)
    kinds = [reasons[n] for n in reasons]
    assert kinds.count("aktuell") == 10
    hourly = [datetime.fromisoformat(n) for n, k in reasons.items() if k == "stündlich"]
    assert all(h.weekday() < 5 for h in hourly)                     # Wochenende zählt nicht
    assert len({datetime.fromisoformat(n).date() for n in reasons}) == 28
