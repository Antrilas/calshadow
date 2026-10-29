"""Integrationstests gegen einen echten CalDAV-Server (Radicale), siehe conftest.py."""

import zipfile
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

from app import backup, db, sync

from .conftest import add_event, day


def run(dry_run: bool = False) -> dict:
    return db.get_run(sync.run_sync("test", dry_run))


def target_events(dst) -> dict[str, str]:
    """UID -> iCalendar-Text aller Termine im beruflichen Kalender."""
    result = {}
    for obj in dst.events():
        ical = Calendar.from_ical(obj.data)
        result[str(ical.walk("VEVENT")[0]["UID"])] = str(obj.data)
    return result


def own(events: dict) -> list[str]:
    return [uid for uid in events if uid.startswith("calshadow-")]


@pytest.mark.parametrize("uid_filter", [True, False])
def test_sync_lifecycle(calendars, uid_filter):
    src, dst = calendars
    db.save_settings({"target_uid_filter": uid_filter})
    add_event(src, "a", f":{day(1)}T100000", f":{day(1)}T110000")
    add_event(src, "b", f":{day(1)}T190000", f":{day(1)}T200000")          # außerhalb Arbeitszeit
    add_event(src, "c", f":{day(2)}T120000", f":{day(2)}T130000", "RRULE:FREQ=WEEKLY;COUNT=3\n")

    assert run(dry_run=True)["created"] == 4 and own(target_events(dst)) == []   # Dry-Run schreibt nichts
    first = run()
    assert first["status"] == "success" and first["created"] == 4
    assert run()["unchanged"] == 4                                              # idempotent

    src.event_by_uid("a").delete()
    second = run()
    assert second["deleted"] == 1 and len(own(target_events(dst))) == 3


def test_moved_event_updates_blocker(calendars):
    src, dst = calendars
    add_event(src, "m", f":{day(1)}T100000", f":{day(1)}T110000")
    run()
    src.event_by_uid("m").delete()
    add_event(src, "m", f":{day(1)}T140000", f":{day(1)}T150000")
    result = run()
    assert result["updated"] == 1 and result["created"] == 0
    (blocker,) = [Calendar.from_ical(t).walk("VEVENT")[0] for u, t in target_events(dst).items() if u.startswith("calshadow-")]
    assert blocker["DTSTART"].dt.astimezone(ZoneInfo("Europe/Berlin")).hour == 14


def test_foreign_and_forged_events_stay_untouched(calendars):
    src, dst = calendars
    add_event(src, "p", f":{day(1)}T100000", f":{day(1)}T110000")
    add_event(dst, "kollege", f":{day(1)}T110000", f":{day(1)}T120000", summary="Privater Blocker")
    add_event(dst, "calshadow-" + "0" * 40, f":{day(1)}T150000", f":{day(1)}T160000", "X-CALSHADOW:gefaelscht\n")
    before = target_events(dst)
    run()
    src.event_by_uid("p").delete()
    run()
    after = target_events(dst)
    assert after["kollege"] == before["kollege"]
    assert after["calshadow-" + "0" * 40] == before["calshadow-" + "0" * 40]
    assert own(after) == ["calshadow-" + "0" * 40]


def test_private_calendar_is_never_modified(calendars):
    src, _ = calendars
    add_event(src, "p", f":{day(1)}T100000", f":{day(1)}T110000")
    before = {str(o.url): str(o.data) for o in src.events()}
    db.save_settings({"backup_enabled": True, "backup_min_interval_minutes": 0})
    run()
    sync.manual_backup("source")
    assert {str(o.url): str(o.data) for o in src.events()} == before


def test_freebusy_mode_gives_same_blockers(calendars):
    src, dst = calendars
    add_event(src, "a", f":{day(1)}T100000", f":{day(1)}T110000")
    add_event(src, "f", f":{day(1)}T120000", f":{day(1)}T130000", "TRANSP:TRANSPARENT\n")
    events_mode = run()
    db.save_settings({"source_mode": "freebusy"})
    fb_run = run()
    assert fb_run["status"] == "success"
    # Kennungen wechseln einmalig, Anzahl bleibt gleich
    assert events_mode["created"] == 1 and fb_run["created"] == 1 and fb_run["deleted"] == 1
    assert len(own(target_events(dst))) == 1


def test_backup_before_sync_contains_calendar(calendars):
    src, dst = calendars
    add_event(dst, "kollege", f":{day(1)}T110000", f":{day(1)}T120000")
    db.save_settings({"backup_enabled": True, "backup_min_interval_minutes": 0})
    run(dry_run=True)
    assert backup.list_backups() == []                       # Dry-Run sichert nicht
    run()
    items = backup.list_backups()
    assert len(items) == 1 and not items[0]["manual"]
    with zipfile.ZipFile(f"{backup.BACKUP_DIR}/{items[0]['name']}") as z:
        assert "kollege" in z.read("kalender.ics").decode()


def test_cleanup_removes_only_own_blockers(calendars):
    src, dst = calendars
    add_event(src, "a", f":{day(1)}T100000", f":{day(1)}T110000")
    add_event(dst, "kollege", f":{day(1)}T110000", f":{day(1)}T120000")
    db.save_settings({"enabled": True})
    run()
    assert sync.cleanup_blockers(False)["count"] == 1
    result = sync.cleanup_blockers(True, disable_sync=True)
    assert result["deleted"] == 1 and result["sync_disabled"] is True
    assert list(target_events(dst)) == ["kollege"]
    assert db.get_settings()["enabled"] is False
    assert any(b["manual"] for b in backup.list_backups())   # Sicherung vor dem Aufräumen
