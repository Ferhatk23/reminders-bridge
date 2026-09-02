"""Zentrale Konfiguration, ausschliesslich aus der Umgebung.

Alles hat einen brauchbaren Standardwert ausser den iCloud-Zugangsdaten -
die App startet also auch ohne Konfiguration und sagt dann, was fehlt.
"""
from __future__ import annotations

import os
from datetime import timedelta, timezone


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# -- iCloud -----------------------------------------------------------------
APPLE_ID: str = os.getenv("ICLOUD_USERNAME", "").strip()
SESSION_DIR: str = os.getenv("ICLOUD_SESSION_DIR", "/session")

# -- Speicher ---------------------------------------------------------------
CACHE_DB: str = os.getenv("CACHE_DB", "/data/cache.db")

# -- Sync -------------------------------------------------------------------
# Ein voller CloudKit-Durchlauf kostet mehrere Sekunden, ein Delta-Sync
# Bruchteile davon. Deshalb zwei Intervalle statt einem.
SYNC_DELTA_SECONDS: int = _int("SYNC_DELTA_SECONDS", 45)
SYNC_FULL_SECONDS: int = _int("SYNC_FULL_SECONDS", 300)
# Gesynct wird nur, solange jemand die API benutzt - sonst funkt der Dienst
# rund um die Uhr Apple an, ohne dass es jemanden interessiert.
SYNC_ACTIVE_WINDOW: int = _int("SYNC_ACTIVE_WINDOW", 600)

# -- HTTP -------------------------------------------------------------------
# Leer = keine Authentifizierung. Das ist nur vertretbar, wenn ein Reverse
# Proxy davor schon authentifiziert (im Homelab z.B. tinyauth).
API_TOKEN: str = os.getenv("API_TOKEN", "").strip()
CORS_ORIGINS: list[str] = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
]
SERVE_WEB: bool = _bool("SERVE_WEB", True)
WEB_ROOT: str = os.getenv("WEB_ROOT", "/web")

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# -- Zeitzone ---------------------------------------------------------------
TZ_NAME: str = os.getenv("TZ", "Europe/Berlin")
try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception:  # pragma: no cover - ohne tzdata
    LOCAL_TZ = timezone(timedelta(hours=1))

# Apple rechnet in diesen Datensaetzen ab 2001-01-01, nicht ab 1970.
APPLE_EPOCH = 978307200
