"""Anwendungsobjekt: Fehlerbehandlung, CORS, Hintergrund-Sync, Routen.

Die Weboberfläche ist bewusst **kein** Teil dieser Anwendung — sie spricht nur
über HTTP mit ihr. Aus Bequemlichkeit kann dieser Prozess sie mit ausliefern
(``SERVE_WEB=true``); wer beides getrennt betreiben will, setzt das auf false
und stellt die Dateien aus ``web/`` anderswo bereit.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .icloud import ICloudError, NotConfigured, Unsupported, store
from .routes import router
from .sync import syncer

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reminders")

DESCRIPTION = """
REST-Schnittstelle zu **Apple Reminders** über die CloudKit-API von iCloud —
ohne Mac, ohne CalDAV.

* **Lesen** kommt aus einem lokalen SQLite-Cache, den ein Hintergrund-Thread
  aktuell hält. Antwortzeiten im Millisekundenbereich.
* **Schreiben** geht synchron nach iCloud; erst der Erfolg landet im Cache.
  iCloud bleibt die einzige Wahrheit, es gibt keinen Zwei-Wege-Sync.

Authentifizierung ist optional: ist `API_TOKEN` gesetzt, wird ein
`Authorization: Bearer …` erwartet.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    syncer.start()
    yield


app = FastAPI(
    title="Reminders Bridge",
    version="1.0.0",
    description=DESCRIPTION,
    lifespan=lifespan,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url=None,
)

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def _touch(request: Request, call_next):
    # Der Hintergrund-Sync läuft nur, solange die API auch benutzt wird.
    if request.url.path.startswith("/v1/"):
        syncer.touch()
    return await call_next(request)


@app.exception_handler(NotConfigured)
def _not_configured(request: Request, exc: NotConfigured):
    return JSONResponse({"detail": str(exc), "setup": True}, status_code=503)


@app.exception_handler(Unsupported)
def _unsupported(request: Request, exc: Unsupported):
    return JSONResponse({"detail": str(exc), "unsupported": True}, status_code=501)


@app.exception_handler(ICloudError)
def _icloud_error(request: Request, exc: ICloudError):
    log.warning("iCloud-Fehler: %s", exc)
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.get("/healthz", tags=["Hilfsmittel"], summary="Lebenszeichen")
def healthz():
    return {"ok": True, "configured": store.configured}


app.include_router(router)


# -- Optional: die Weboberfläche gleich mit ausliefern -----------------------
if config.SERVE_WEB and os.path.isdir(config.WEB_ROOT):
    app.mount("/static", StaticFiles(directory=config.WEB_ROOT), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(config.WEB_ROOT, "index.html"))

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(os.path.join(config.WEB_ROOT, "manifest.webmanifest"))

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return FileResponse(os.path.join(config.WEB_ROOT, "favicon.svg"))
