"""Optionale Bearer-Token-Authentifizierung.

Ist ``API_TOKEN`` nicht gesetzt, ist die API offen. Das ist Absicht: im
Homelab sitzt oft schon ein authentifizierender Reverse Proxy davor, und ein
zweites Passwort waere dort nur laestig. Sobald die API aber direkt erreichbar
ist, sollte ein Token gesetzt sein.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from . import config


def require_token(authorization: str | None = Header(default=None)) -> None:
    if not config.API_TOKEN:
        return
    expected = f"Bearer {config.API_TOKEN}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401,
            detail="Ungültiger oder fehlender API-Token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
