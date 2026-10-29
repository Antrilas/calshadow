# Stufe 1: Abhängigkeiten installieren (pip wird nur hier gebraucht)
FROM python:3.12-alpine AS build

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-compile --target /deps -r /tmp/requirements.txt \
 && find /deps -depth \( -name "__pycache__" -o -name "tests" -o -name "*.pyi" \) -exec rm -rf {} +

# Stufe 2: schlankes Laufzeit-Image ohne pip und Build-Werkzeuge
FROM python:3.12-alpine

ARG VERSION=dev
LABEL org.opencontainers.image.title="CalShadow" \
      org.opencontainers.image.description="Private Termine als anonyme Blocker im beruflichen Kalender" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/Antrilas/calshadow"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/deps \
    CALSHADOW_VERSION=${VERSION} \
    DB_PATH=/data/calshadow.db \
    TZ=Europe/Berlin

RUN apk add --no-cache tzdata \
 && pip uninstall -y pip \
 && adduser -D -H -u 1000 app \
 && mkdir -p /data && chown app /data

COPY --from=build /deps /deps
WORKDIR /srv
COPY app ./app
COPY LICENSE ./LICENSE

USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
