"""Web-Oberfläche und Scheduler."""

import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, available_timezones

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import backup, db, sync

BASE = os.path.dirname(__file__)
WEB_USER = os.environ.get("WEB_USER", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")

security = HTTPBasic(auto_error=False)


# Ohne Admin-Anmeldung erreichbar: Healthcheck und die per Lese-Token geschützte Homepage-API
PUBLIC_PATHS = {"/healthz", "/api/homepage"}


def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)):
    if not WEB_PASSWORD or request.url.path in PUBLIC_PATHS:
        return
    ok = creds is not None and secrets.compare_digest(creds.username, WEB_USER) \
        and secrets.compare_digest(creds.password, WEB_PASSWORD)
    if not ok:
        raise HTTPException(401, "Anmeldung erforderlich", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="CalShadow", dependencies=[Depends(auth)], docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


# ---------- Scheduler ----------

class Scheduler:
    def __init__(self):
        self._wake = threading.Event()
        self.next_run: datetime | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def reschedule(self):
        self._wake.set()

    def _initial_next_run(self, s: dict) -> datetime:
        """Beim Start: letzter Sync + Intervall; ein überfälliger Sync wird nach 5 s nachgeholt."""
        now = datetime.now(timezone.utc)
        last = db.last_real_run()
        if last and last["finished"]:
            due = datetime.fromisoformat(last["finished"]) + timedelta(minutes=int(s["interval_minutes"]))
            if due > now:
                return due
        return now + timedelta(seconds=5)

    def _loop(self):
        s = db.get_settings()
        self.next_run = self._initial_next_run(s) if s["enabled"] else None
        while True:
            s = db.get_settings()
            now = datetime.now(timezone.utc)
            if not s["enabled"]:
                self.next_run = None
                delay = 3600
            else:
                # Speichern der Einstellungen behält den geplanten Lauf bei; nur beim Einschalten
                # wird neu geplant – und zwar ein volles Intervall später, nie sofort.
                if self.next_run is None:
                    self.next_run = now + timedelta(minutes=int(s["interval_minutes"]))
                delay = max(0.0, (self.next_run - now).total_seconds())
            if self._wake.wait(timeout=delay):
                self._wake.clear()
                continue
            if not db.get_settings()["enabled"]:
                continue
            try:
                sync.run_sync("schedule")
            except Exception as e:  # Scheduler darf nie sterben
                print(f"Scheduler-Fehler: {e}", flush=True)
            self.next_run = datetime.now(timezone.utc) + timedelta(minutes=int(db.get_settings()["interval_minutes"]))


scheduler = Scheduler()


@app.on_event("startup")
def startup():
    db.init()
    scheduler.start()


# ---------- Template-Helfer ----------

def local_tz() -> ZoneInfo:
    return ZoneInfo(db.get_settings()["timezone"])


def fmt_dt(value, fmt="%d.%m.%Y %H:%M"):
    if not value:
        return "–"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(local_tz()).strftime(fmt)


def fmt_ago(value):
    if not value:
        return "nie"
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    sec = int((datetime.now(timezone.utc) - dt).total_seconds())
    if sec < 0:
        sec = -sec
        if sec < 60:
            return f"in {sec} s"
        return f"in {sec // 60} min"
    if sec < 60:
        return f"vor {sec} s"
    if sec < 3600:
        return f"vor {sec // 60} min"
    if sec < 86400:
        return f"vor {sec // 3600} h"
    return f"vor {sec // 86400} Tagen"


def duration(run):
    if not run.get("finished"):
        return "läuft…"
    d = datetime.fromisoformat(run["finished"]) - datetime.fromisoformat(run["started"])
    return f"{d.total_seconds():.1f} s"


templates.env.filters["dt"] = fmt_dt
templates.env.filters["ago"] = fmt_ago
templates.env.globals["duration"] = duration
templates.env.globals["weekday_names"] = sync.WEEKDAY_NAMES
# Version und Quellcode-Link für die Fußzeile (AGPL-3.0, Abschnitt 13: Quellcode für Netzwerknutzer anbieten).
# Wer eine veränderte Version betreibt, setzt CALSHADOW_SOURCE_URL auf seinen eigenen Quellcode.
templates.env.globals["app_version"] = os.environ.get("CALSHADOW_VERSION", "dev")
templates.env.globals["source_url"] = os.environ.get("CALSHADOW_SOURCE_URL", "https://github.com/Antrilas/calshadow")

HISTORY_BARS = 90  # Anzahl Balken im Sync-Verlauf (Dashboard)


def _timezone_groups() -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for tz in sorted(available_timezones()):
        if "/" in tz and not tz.startswith(("Etc/", "SystemV/", "US/", "posix/", "right/")):
            groups.setdefault(tz.split("/", 1)[0], []).append(tz)
    return sorted(groups.items(), key=lambda g: (g[0] != "Europe", g[0])) + [("Sonstige", ["UTC"])]


TIMEZONE_GROUPS = _timezone_groups()
templates.env.globals["history_bars"] = HISTORY_BARS


def next_sync_info() -> dict:
    """Nächster automatischer Sync und ob dabei eine Sicherung entsteht (Mindestabstand abgelaufen)."""
    nxt = scheduler.next_run
    if not nxt:
        return {"next_sync": None, "with_backup": False}
    s = db.get_settings()
    last = backup.last_automatic_backup_time()
    due = last + timedelta(minutes=int(s["backup_min_interval_minutes"])) if last else None
    return {"next_sync": nxt.isoformat(), "with_backup": bool(s["backup_enabled"]) and (due is None or due <= nxt)}


def render(request: Request, name: str, **ctx):
    ctx.setdefault("running", sync.is_running())
    ctx.setdefault("auth_enabled", bool(WEB_PASSWORD))
    for key, value in next_sync_info().items():
        ctx.setdefault(key, value)
    return templates.TemplateResponse(request, name, ctx)


# ---------- Seiten ----------

@app.get("/")
def dashboard(request: Request):
    s = db.get_settings()
    tz = ZoneInfo(s["timezone"])
    blockers = []
    now = datetime.now(timezone.utc)
    for b in db.get_blockers():
        start, end = datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])
        if end < now:
            continue
        ls = start.astimezone(tz)
        blockers.append({
            "day": f"{sync.WEEKDAY_NAMES[ls.weekday()]} {ls:%d.%m.}",
            "time": f"{ls:%H:%M} – {end.astimezone(tz):%H:%M}",
            "is_today": ls.date() == datetime.now(tz).date(),
        })
    configured = bool(s["source_calendars"].strip() and s["target_calendar"])
    return render(
        request, "dashboard.html",
        settings=s, runs=db.get_runs(5), last=db.last_real_run(),
        history=list(reversed(db.query_runs("live", "", HISTORY_BARS, 0)[0])),
        next_run=scheduler.next_run.isoformat() if scheduler.next_run else None,
        blockers=blockers, configured=configured,
    )


@app.get("/logs")
def logs_page(request: Request, run: int | None = None, level: str = "", cleared: int | None = None):
    runs = db.get_runs(100)
    if run is None and runs:
        run = runs[0]["id"]
    levels = [lvl for lvl in level.split(",") if lvl]
    return render(
        request, "logs.html", settings=db.get_settings(),
        runs=runs, current=db.get_run(run) if run else None,
        logs=db.get_logs(run, levels) if run else [], level=level, cleared=cleared,
    )


@app.post("/logs/clear")
def logs_clear(back: str = ""):
    logs, runs = db.clear_logs()
    if back == "settings":
        return RedirectResponse(f"/settings?logs_cleared={logs}#logs", status_code=303)
    return RedirectResponse(f"/logs?cleared={logs}", status_code=303)


@app.post("/logs/run/{run_id}/delete")
def logs_delete_run(run_id: int, back: str = ""):
    if not db.delete_run(run_id):
        raise HTTPException(400, "Lauf nicht gefunden oder läuft gerade")
    return RedirectResponse(back if back.startswith("/runs") else "/logs", status_code=303)


RUNS_PER_PAGE = 50


@app.get("/runs")
def runs_page(request: Request, kind: str = "", status: str = "", page: int = 1):
    page = max(1, page)
    runs, total, agg = db.query_runs(kind, status, RUNS_PER_PAGE, (page - 1) * RUNS_PER_PAGE)
    pages = max(1, -(-total // RUNS_PER_PAGE))
    return render(request, "runs.html", runs=runs, total=total, agg=agg, kind=kind, status=status,
                  page=page, pages=pages)


@app.get("/settings")
def settings_page(request: Request, saved: int = 0, logs_cleared: int | None = None):
    return render(request, "settings.html", s=db.get_settings(), saved=saved, logs_cleared=logs_cleared,
                  timezone_groups=TIMEZONE_GROUPS)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    cur = db.get_settings()
    new = {}
    for key in ("source_name", "target_name", "source_url", "source_username", "source_calendars", "target_url",
                "target_username", "target_calendar", "work_start", "work_end", "filter_mode",
                "blocker_title", "blocker_class", "timezone", "kuma_backup_push_url", "source_mode",
                "kuma_sync_push_url", "blocker_description"):
        new[key] = str(form.get(key, cur[key])).strip()
    new["blocker_description"] = new["blocker_description"].replace("\r\n", "\n")[:1000]
    for key in ("source_name", "target_name"):
        new[key] = new[key][:60] or db.DEFAULT_SETTINGS[key]
    for key in ("source_password", "target_password"):
        val = str(form.get(key, ""))
        if val:  # leer = unverändert
            new[key] = val
    for key in ("days_back", "days_ahead", "interval_minutes", "max_deletions", "log_retention_days", "stale_factor",
                "backup_min_interval_minutes", "backup_keep_last", "backup_keep_hourly",
                "backup_keep_daily"):
        try:
            new[key] = max(0, int(form.get(key, cur[key])))
        except ValueError:
            new[key] = cur[key]
    new["interval_minutes"] = max(1, new["interval_minutes"])
    if new["source_mode"] not in ("events", "freebusy"):
        new["source_mode"] = cur["source_mode"]
    new["stale_factor"] = max(1, new["stale_factor"])
    new["backup_keep_last"] = max(1, new["backup_keep_last"])
    for key in ("enabled", "include_all_day", "skip_transparent", "skip_tentative", "backup_enabled",
                "target_uid_filter",
                "log_event_titles",
                "backup_hourly_skip_weekend"):
        new[key] = form.get(key) == "on"
    if new["source_mode"] == "freebusy":
        new["log_event_titles"] = False  # ohne Termininhalte gibt es keine Titel
    new["weekdays"] = sorted(int(d) for d in form.getlist("weekdays"))
    try:
        ZoneInfo(new["timezone"])
    except Exception:
        new["timezone"] = cur["timezone"]
    db.save_settings(new)
    db.add_log(None, "INFO", "Einstellungen gespeichert")
    scheduler.reschedule()
    tab = re.sub(r"[^a-z]", "", str(form.get("tab", "")))[:20]
    return RedirectResponse(f"/settings?saved=1{'#' + tab if tab else ''}", status_code=303)


@app.get("/backups")
def backups_page(request: Request):
    s = db.get_settings()
    items = backup.list_backups()
    reasons = backup.retention_reasons([b for b in items if not b["manual"]], s)
    for b in items:
        b["kind"] = "manuell" if b["manual"] else reasons.get(b["name"], "wird entfernt")
    configured = {"target": bool(s["target_calendar"]), "source": bool(s["source_calendars"].strip())}
    return render(request, "backups.html", backups=items, s=s, configured=configured,
                  total_size=sum(b["size"] for b in items))


@app.post("/api/backup/{side}")
async def api_manual_backup(side: str):
    if side not in ("source", "target"):
        raise HTTPException(404)
    try:
        info = await _in_thread(sync.manual_backup, side)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "message": f"Sicherung erstellt: {info['objects']} Objekte, "
                                   f"{info['size'] / 1024:.0f} KB, {info['duration']:.1f} s"}


@app.post("/backups/{name}/delete")
def backup_delete(name: str):
    if not backup.delete_backup(name):
        raise HTTPException(404, "Sicherung nicht gefunden")
    db.add_log(None, "INFO", f"Sicherung gelöscht: {name}")
    return RedirectResponse("/backups", status_code=303)


@app.get("/backups/{name}")
def backup_download(name: str):
    path = backup.backup_path(name)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, media_type="application/zip", filename=name)


@app.post("/settings/homepage-token")
def regenerate_homepage_token():
    db.save_settings({"homepage_token": secrets.token_urlsafe(32)})
    db.add_log(None, "INFO", "Homepage-Lese-Token neu erzeugt")
    return RedirectResponse("/settings?saved=1#homepage", status_code=303)


# ---------- Monitoring ----------

@app.post("/api/kuma-test/{kind}")
async def api_kuma_test(kind: str, request: Request):
    if kind not in ("backup", "sync"):
        raise HTTPException(404)
    url = str((await request.form()).get(f"kuma_{kind}_push_url") or "").strip()
    if not url:
        return {"ok": False, "message": "Bitte zuerst die Push-URL eintragen"}
    from . import notify
    err = await _in_thread(notify.kuma_push, url, True, "Test aus CalShadow", None)
    return {"ok": not err, "message": err or "Push angekommen – in Uptime Kuma sollte jetzt ein grüner Eintrag stehen"}

@app.get("/healthz")
def healthz():
    problems = []
    try:
        db.get_settings()
    except Exception as e:
        problems.append(f"Datenbank: {e}")
    if not scheduler.is_alive():
        problems.append("Scheduler läuft nicht")
    if problems:
        return JSONResponse({"status": "error", "problems": problems}, status_code=503)
    return {"status": "ok"}


@app.get("/api/homepage")
def api_homepage(request: Request):
    s = db.get_settings()
    token = request.headers.get("X-API-Key", "")
    if not s["homepage_token"] or not secrets.compare_digest(token, s["homepage_token"]):
        raise HTTPException(401, "Ungültiger oder fehlender X-API-Key")

    last = db.last_real_run()
    last_ok = db.last_real_run(status="success")
    now = datetime.now(timezone.utc)
    stale_after = timedelta(minutes=int(s["interval_minutes"]) * max(1, int(s["stale_factor"])))

    if not (s["source_calendars"].strip() and s["target_calendar"]):
        status = "Nicht eingerichtet"
    elif not s["enabled"]:
        status = "Deaktiviert"
    elif sync.is_running():
        status = "Läuft"
    elif last and last["status"] == "error":
        status = "Fehler"
    elif not last_ok or now - datetime.fromisoformat(last_ok["finished"]) > stale_after:
        status = "Veraltet"
    else:
        status = "OK"

    future_blockers = sum(1 for b in db.get_blockers() if datetime.fromisoformat(b["end"]) >= now)
    return {
        "status": status,
        "private_calendar": s["source_name"],
        "work_calendar": s["target_name"],
        "last_sync": last["finished"] if last else None,
        "last_success": last_ok["finished"] if last_ok else None,
        "next_sync": scheduler.next_run.isoformat(timespec="seconds") if scheduler.next_run else None,
        "blockers": future_blockers,
        "last_error": last["error"] if last and last["status"] == "error" else None,
    }


# ---------- API ----------

@app.post("/api/sync")
def api_sync(dry_run: int = 0):
    if sync.is_running():
        return JSONResponse({"ok": False, "message": "Es läuft bereits ein Sync"}, status_code=409)
    threading.Thread(
        target=sync.run_sync, args=("manuell (Dry-Run)" if dry_run else "manuell", bool(dry_run)),
        daemon=True,
    ).start()
    return {"ok": True}


@app.get("/api/status")
def api_status():
    runs = db.get_runs(1)
    return {"running": sync.is_running(), "latest_run": runs[0] if runs else None, **next_sync_info()}


@app.get("/api/logs")
def api_logs(run: int, after: int = 0, level: str = ""):
    levels = [lvl for lvl in level.split(",") if lvl]
    return {"logs": db.get_logs(run, levels, after_id=after), "run": db.get_run(run)}


@app.post("/api/test/{side}")
async def api_test(side: str, request: Request):
    if side not in ("source", "target"):
        raise HTTPException(404)
    form = await request.form()
    s = db.get_settings()
    url = str(form.get(f"{side}_url") or "").strip()
    if side == "target" and not url:
        url = str(form.get("target_calendar") or "").strip()
    user = str(form.get(f"{side}_username") or "").strip()
    password = str(form.get(f"{side}_password") or "") or s[f"{side}_password"]
    if not url:
        return {"ok": False, "message": "Bitte zuerst die Server-URL eintragen"}
    try:
        cals = await _in_thread(sync.list_calendars, url, user, password, side == "source")
        return {"ok": True, "calendars": cals}
    except Exception as e:
        return {"ok": False, "message": sync.describe_error(e)}


def _target_settings(form) -> dict:
    """Gespeicherte Einstellungen, überschrieben mit den (evtl. ungespeicherten) Kalender-Feldern."""
    s = db.get_settings()
    for key in ("target_name", "target_url", "target_username", "target_calendar", "source_url",
                "source_calendars"):
        if form.get(key) is not None:
            s[key] = str(form.get(key)).strip()
    if form.get("target_password"):
        s["target_password"] = str(form.get("target_password"))
    return s


@app.post("/api/cleanup/{action}")
async def api_cleanup(action: str, request: Request):
    if action not in ("preview", "run"):
        raise HTTPException(404)
    form = await request.form()
    try:
        if action == "preview":
            return {"ok": True, **(await _in_thread(sync.cleanup_blockers, False))}
        result = await _in_thread(sync.cleanup_blockers, True, form.get("disable_sync") == "on")
    except Exception as e:
        return {"ok": False, "message": str(e)}
    scheduler.reschedule()
    return {"ok": True, **result}


@app.post("/api/test-blocker/{action}")
async def api_test_blocker(action: str, request: Request):
    fns = {"status": sync.test_blocker_status, "create": sync.create_test_blocker,
           "delete": sync.delete_test_blocker}
    if action not in fns:
        raise HTTPException(404)
    s = _target_settings(await request.form())
    try:
        result = await _in_thread(fns[action], s)
    except Exception as e:
        return {"ok": False, "message": f"{e}"}
    if action != "status":
        msg = {"create": "Test-Blocker angelegt", "delete": "Test-Blocker gelöscht"}[action]
        db.add_log(None, "SUCCESS", f"{msg}{': ' + result['label'] if result.get('label') else ''}")
    return {"ok": True, **result}


async def _in_thread(fn, *args):
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)
