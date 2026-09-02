FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ICLOUD_SESSION_DIR=/session \
    CACHE_DB=/data/cache.db \
    WEB_ROOT=/web

WORKDIR /srv

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/reminders_api ./reminders_api
# Die Oberfläche liegt mit im Image, damit ein einzelner Container genügt.
# Wer API und Web getrennt betreiben will, setzt SERVE_WEB=false.
COPY web/public /web

VOLUME ["/session", "/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"

CMD ["uvicorn", "reminders_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
