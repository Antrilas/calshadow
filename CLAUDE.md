# CalShadow – Hinweise für die Arbeit am Code

CalShadow überträgt Termine aus einem privaten CalDAV-Kalender (z. B. Posteo) als anonyme Blocker in einen
beruflichen CalDAV-Kalender (z. B. SOGo). Python 3.12, FastAPI, Jinja2, caldav, SQLite, Docker (Alpine).
Oberfläche, Logs, README und Commit-Nachrichten sind **deutsch**.

## Aufbau

| Datei | Inhalt |
|---|---|
| `app/sync.py` | Sync-Logik: Quelle lesen (Termine oder Frei/Belegt), Regeln anwenden, Blocker schreiben; DAV-Clients mit Schreibsperren; Test-Blocker; Aufräumen; manuelle Sicherung; `describe_error` |
| `app/main.py` | FastAPI-Routen, Basic Auth, Scheduler (Thread), Monitoring-APIs (`/healthz`, `/api/homepage`) |
| `app/db.py` | SQLite: `settings` (JSON-Werte, `DEFAULT_SETTINGS`), `runs`, `logs`, Blocker-Snapshot |
| `app/backup.py` | ZIP-Sicherungen, Aufbewahrungsregeln (`retention_reasons`), Dateinamen `backup-<name>-<zeit>-run<n>.zip` bzw. `…-manuell.zip` |
| `app/notify.py` | Push an Uptime Kuma |
| `app/templates/`, `app/static/` | Oberfläche; Schrift Sora lokal (`static/fonts`), Markenbilder in `static/brand` |
| `tests/` | Unit-Tests (ohne Server) und Integrationstests gegen Radicale |
| `deploy/compose.yaml` | Beispiel mit fertigem Image; `docker-compose.yml` baut aus dem Quellcode |

## Sicherheitsregeln – nie aufweichen

- **Privater Kalender nur lesend**: `ReadOnlyDAVClient` erlaubt nur `GET/HEAD/OPTIONS/PROPFIND/REPORT`, geprüft in
  `request()` **und** in der requests-Session. Jeder Zugriff auf die Quelle läuft über `_source_client()`.
- **Beruflicher Kalender**: `TargetDAVClient` erlaubt `PUT/DELETE` nur auf per `allow_write()` freigegebene
  Objekt-URLs direkt im Zielkalender. Neue Blocker mit `If-None-Match: *`; Ändern/Löschen nur über `_modify_own()`
  (frisch laden, `parse_own_blocker` erneut prüfen, `If-Match`-ETag).
- **Eigener Blocker** = UID `calshadow-<40 hex>` + gültige HMAC-Signatur in `X-CALSHADOW` (Schlüssel
  `instance_secret`) + genau ein VEVENT ohne RRULE/ATTENDEE/ORGANIZER. Der Titel zählt nie.
- **Keine privaten Inhalte** im beruflichen Kalender, in Uptime-Kuma-Meldungen oder in den Docker-Logs. Termintitel
  nur in der eigenen Log-Datenbank und nur mit der Option `log_event_titles` (im Frei/Belegt-Modus gesperrt).
- `check_target_is_not_source()`, Sicherheitsstopp `max_deletions` und „Sicherung fehlgeschlagen → nichts schreiben“
  bleiben erhalten. Dry-Runs schreiben nichts und sichern nicht.
- Fehlermeldungen für Nutzer über `describe_error()` statt roher Exceptions.

## Konventionen

- **Neue Einstellung**: in `db.DEFAULT_SETTINGS`, in der passenden Schlüsselliste von `settings_save()` in `main.py`
  (Text, Zahl oder Checkbox – fehlende Checkboxen bedeuten `False`) und im Template. Die Einstellungsseite ist **ein**
  Formular, das Menü blendet nur Bereiche ein; Aktionen außerhalb des Formulars über `postAction()` bzw. `fetch`.
- **Speichern löst keinen Sync aus**; der Scheduler behält `next_run`, nur Einschalten plant ein volles Intervall später.
- Passwortfelder bleiben gesperrt, bis „Ändern“ geklickt wird (Schutz vor Passwort-Managern).
- Läufe mit `trigger == "Aufräumen"` zählen nicht als Sync (Dashboard, Homepage-API, Sync-Verlauf).
- Manuelle Sicherungen melden nichts an Uptime Kuma und fallen nicht unter die Aufbewahrungsregeln.
- CSS: Farb-Tokens in `:root`, Hell/Dunkel/Auto über `data-theme`; `[hidden]` ist global `display:none !important`.
  Keine externen CDNs oder Schriften.
- Log-Zeilen für Termine über `_line()` (Datum/Uhrzeit untereinander, Details und ggf. Titel am Ende).

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
radicale --config tests/radicale/config &
python -m pytest tests
```

Ohne erreichbaren Radicale werden die Integrationstests übersprungen. Quelle und Ziel nutzen `127.0.0.1` bzw.
`localhost`, weil gleiche Hosts abgelehnt werden. Nach Änderungen an der Oberfläche zusätzlich im Browser bzw. per
Screenshot prüfen.

## Release und Repository

- Release = Git-Tag `vX.Y.Z` pushen → `.github/workflows/release.yml` testet und veröffentlicht amd64+arm64 auf
  `ghcr.io/antrilas/calshadow` und `antrilas/calshadow` (Docker Hub). **Keine** GitHub-Release-Einträge anlegen.
- Commits mit der Identität `Antrilas <Antrilas@users.noreply.github.com>` (im Repository setzen, nie eine
  private Mailadresse).
- Dependabot bündelt Updates; Hauptversionen von caldav, icalendar und recurring-ical-events werden bewusst ignoriert.

## Offen

- Umstieg auf caldav 3 / icalendar 7 / recurring-ical-events 3: Die Schreibsperren setzen an caldav-Interna an
  (`DAVClient.request`, `session.request`, `DAVResponse._raw`) – gezielt umbauen und mit allen Tests absichern.
- `@app.on_event("startup")` ist in FastAPI veraltet → bei Gelegenheit auf `lifespan` umstellen.
