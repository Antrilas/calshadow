"""Push-Benachrichtigungen an Uptime Kuma."""

import urllib.request
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def kuma_push(url: str, up: bool, msg: str, ping_ms: float | None = None) -> str | None:
    """Sendet einen Push an eine Uptime-Kuma-Push-URL. Gibt None bei Erfolg oder eine Fehlermeldung zurück.

    Die URL kann so eingetragen werden, wie Kuma sie anzeigt (inkl. ?status=up&msg=OK&ping=);
    status, msg und ping werden hier ersetzt.
    """
    if not url.strip():
        return None
    p = urlparse(url.strip())
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    query.update({"status": "up" if up else "down", "msg": msg[:250],
                  "ping": "" if ping_ms is None else str(round(ping_ms))})
    full = urlunparse(p._replace(query=urlencode(query)))
    try:
        with urllib.request.urlopen(full, timeout=10) as r:
            body = r.read(500).decode("utf-8", "replace")
        if '"ok":false' in body.replace(" ", ""):
            return f"Kuma meldet Fehler: {body}"
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"
