"""Schemas der HTTP-Schnittstelle.

Bewusst getrennt von den internen Dataclasses in icloud.py: was hier steht,
ist der oeffentliche Vertrag und soll sich nicht aendern, nur weil intern
umgebaut wird.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# Apple: 1 = hoch, 5 = mittel, 9 = niedrig, 0 = keine.
Priority = Literal[0, 1, 5, 9]


class Section(BaseModel):
    """Ein Trenner innerhalb einer Liste (in iOS „Abschnitt")."""

    id: str
    name: str


class TaskList(BaseModel):
    id: str
    name: str
    color: Optional[str] = None
    open_count: int = 0
    done_count: int = 0
    readonly: bool = False
    sections: list[Section] = Field(default_factory=list)


class Task(BaseModel):
    id: str
    list_id: str
    list_name: str = ""
    title: str
    notes: str = ""
    priority: Priority = 0
    due: Optional[str] = Field(
        None, description='"YYYY-MM-DD" für ganztägig, sonst "YYYY-MM-DDTHH:MM" in lokaler Zeit'
    )
    due_has_time: bool = False
    completed: bool = False
    completed_at: Optional[str] = None
    flagged: bool = False
    section_id: str = ""
    section_name: str = ""
    section_order: int = 9999
    recurring: bool = Field(False, description="Hat eine Wiederholungsregel — nur lesbar")
    linked: bool = Field(False, description="Teil einer Unteraufgaben-Kette — nur lesbar")
    movable: bool = Field(
        True, description="Falls false, würde ein Listenwechsel Daten verlieren; block_reason nennt welche"
    )
    block_reason: str = ""
    overdue: bool = False
    today: bool = False
    days: Optional[int] = Field(None, description="Tage bis zur Fälligkeit, negativ = überfällig")
    url: str = ""


class TaskCreate(BaseModel):
    list_id: str
    text: str = Field(..., description="Titel; bei parse=true wird Datum/Priorität daraus gelesen")
    parse: bool = Field(True, description="Deutsche Schnelleingabe anwenden (siehe GET /v1/parse)")
    due: Optional[str] = None
    priority: Optional[Priority] = None
    notes: str = ""
    flagged: bool = False


class TaskUpdate(BaseModel):
    """Nur gesetzte Felder werden geändert. ``due: null`` entfernt das Datum."""

    title: Optional[str] = None
    notes: Optional[str] = None
    priority: Optional[Priority] = None
    due: Optional[str] = None
    completed: Optional[bool] = None
    flagged: Optional[bool] = None

    model_config = {"extra": "forbid"}


class MoveRequest(BaseModel):
    target_list_id: str


class SectionRequest(BaseModel):
    section_id: Optional[str] = Field(
        None, description="null entfernt die Zuordnung (in iOS „Andere“)"
    )


class LoginRequest(BaseModel):
    password: str = Field(..., description="Das echte Apple-ID-Passwort, nicht app-spezifisch")


class CodeRequest(BaseModel):
    code: str = Field(..., description="Sechsstelliger 2FA-Code vom Apple-Gerät")


class SessionStatus(BaseModel):
    valid: bool
    reason: str = ""
    apple_id: str = ""
    can_login: bool = False


class LoginResult(BaseModel):
    needs_2fa: bool = False


class SyncStatus(BaseModel):
    syncing: bool
    last_sync: float = 0
    age: Optional[int] = Field(None, description="Sekunden seit dem letzten erfolgreichen Sync")
    error: str = ""


class ParseResult(BaseModel):
    title: str
    due: Optional[str] = None
    priority: Priority = 0


class ViewCounts(BaseModel):
    today: int = 0
    upcoming: int = 0
    nodate: int = 0
    all: int = 0
    flagged: int = 0


class Ok(BaseModel):
    ok: bool = True
