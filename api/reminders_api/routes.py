"""Die HTTP-Schnittstelle.

Alles unter ``/v1``. Lesen kommt aus dem SQLite-Cache und ist damit schnell
genug, um pro Seitenaufbau mehrfach aufgerufen zu werden; geschrieben wird
synchron gegen iCloud, weil der Aufrufer wissen soll, ob es geklappt hat.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .auth import require_token
from .cache import cache
from .icloud import Task as StoreTask
from .icloud import store
from .models import (
    CodeRequest,
    LoginRequest,
    LoginResult,
    MoveRequest,
    Ok,
    ParseResult,
    SectionRequest,
    SessionStatus,
    SyncStatus,
    Task,
    TaskCreate,
    TaskList,
    TaskUpdate,
    ViewCounts,
)
from .quickadd import parse as quick_parse
from .sync import syncer

router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

VIEWS = ("today", "upcoming", "all", "nodate", "done", "flagged")


# --------------------------------------------------------------------------
# Sitzung
# --------------------------------------------------------------------------

session = APIRouter(prefix="/session", tags=["Sitzung"])


@session.get("", response_model=SessionStatus, summary="Ist die iCloud-Sitzung gültig?")
def session_status():
    return store.session_status()


@session.post("/login", response_model=LoginResult, summary="Mit Apple-ID-Passwort anmelden")
def session_login(body: LoginRequest):
    """Schritt 1 von 2. Das Passwort wird nur durchgereicht, nie gespeichert
    und nie protokolliert; persistiert wird allein die Sitzung von Apple."""
    res = store.login(body.password)
    if not res.get("needs_2fa"):
        syncer.request_full()
    return res


@session.post("/code", response_model=Ok, summary="2FA-Code bestätigen")
def session_code(body: CodeRequest):
    """Schritt 2 von 2. Danach hält die Sitzung laut Apple rund zwei Monate."""
    store.submit_2fa(body.code)
    syncer.request_full()
    return Ok()


router.include_router(session)


# --------------------------------------------------------------------------
# Listen
# --------------------------------------------------------------------------

@router.get("/lists", response_model=list[TaskList], tags=["Listen"],
            summary="Alle Erinnerungslisten samt Trennern")
def get_lists():
    """Anlegen, Umbenennen und Löschen von Listen und Trennern bietet Apples
    CloudKit-API nicht an — das geht nur auf einem Apple-Gerät."""
    _ensure_cache()
    return cache.lists()


# --------------------------------------------------------------------------
# Aufgaben
# --------------------------------------------------------------------------

def _filter(tasks: list[StoreTask], view: str, query: str) -> list[StoreTask]:
    today = date.today()
    out = []
    for t in tasks:
        dd = t.due_dt.date() if t.due_dt else None
        if view == "today":
            keep = (not t.completed) and dd is not None and dd <= today
        elif view == "upcoming":
            keep = (not t.completed) and dd is not None and today <= dd <= today + timedelta(days=7)
        elif view == "nodate":
            keep = (not t.completed) and dd is None
        elif view == "done":
            keep = t.completed
        elif view == "flagged":
            keep = (not t.completed) and (t.flagged or t.priority in (1, 5))
        else:
            keep = not t.completed
        if not keep:
            continue
        if query and query not in f"{t.title}\n{t.notes}\n{t.list_name}".lower():
            continue
        out.append(t)
    return out


def _sorted(tasks: list[StoreTask]) -> list[StoreTask]:
    from datetime import datetime

    from .config import LOCAL_TZ

    rank = {1: 0, 5: 1, 9: 2, 0: 3}
    far = datetime(2999, 1, 1, tzinfo=LOCAL_TZ)
    return sorted(tasks, key=lambda t: (t.completed, t.section_order, t.due_dt or far,
                                        rank.get(t.priority, 3), t.title.lower()))


def _ensure_cache() -> None:
    """Beim allererste Start ist der Cache leer — dann einmal blockierend füllen."""
    if cache.empty:
        syncer.full_sync()


@router.get("/tasks", response_model=list[Task], tags=["Aufgaben"],
            summary="Aufgaben einer Liste oder einer Smart-Ansicht")
def get_tasks(
    list_id: str = Query("", description="Eine konkrete Liste. Schließt `view` aus."),
    view: str = Query("all", description=f"Eine von: {', '.join(VIEWS)}"),
    q: str = Query("", description="Volltextsuche in Titel, Notiz und Listenname"),
    include_completed: bool = Query(False, description="Nur bei `list_id` relevant"),
):
    _ensure_cache()
    query = q.strip().lower()
    if list_id:
        tasks = cache.tasks(list_id)
        if not include_completed:
            tasks = [t for t in tasks if not t.completed]
        if query:
            tasks = [t for t in tasks if query in f"{t.title}\n{t.notes}".lower()]
    else:
        if view not in VIEWS:
            raise HTTPException(400, f"Unbekannte Ansicht {view!r}. Erlaubt: {', '.join(VIEWS)}")
        tasks = _filter(cache.tasks(), view, query)
    return [t.as_dict() for t in _sorted(tasks)]


@router.get("/views", response_model=ViewCounts, tags=["Aufgaben"],
            summary="Anzahl offener Aufgaben je Smart-Ansicht")
def get_views():
    _ensure_cache()
    all_tasks = cache.tasks()
    return {v: len(_filter(all_tasks, v, "")) for v in
            ("today", "upcoming", "nodate", "all", "flagged")}


@router.post("/tasks", response_model=Task, tags=["Aufgaben"], status_code=201,
             summary="Aufgabe anlegen")
def create_task(body: TaskCreate):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Die Erinnerung braucht einen Text.")
    parsed = quick_parse(text) if body.parse else {"title": text, "due": None, "priority": 0}
    task = store.create(
        body.list_id,
        title=parsed["title"],
        due=body.due if body.due is not None else parsed["due"],
        priority=body.priority if body.priority is not None else parsed["priority"],
        notes=body.notes,
        flagged=body.flagged,
    )
    cache.upsert_task(task)
    return task.as_dict()


@router.get("/tasks/{list_id}/{task_id}", response_model=Task, tags=["Aufgaben"],
            summary="Eine Aufgabe")
def get_task(list_id: str, task_id: str):
    _ensure_cache()
    for t in cache.tasks(list_id):
        if t.id == task_id:
            return t.as_dict()
    raise HTTPException(404, "Aufgabe nicht gefunden.")


@router.patch("/tasks/{list_id}/{task_id}", response_model=Task, tags=["Aufgaben"],
              summary="Aufgabe ändern")
def update_task(list_id: str, task_id: str, body: TaskUpdate):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "Nichts zu ändern.")
    if "title" in fields and not str(fields["title"]).strip():
        raise HTTPException(400, "Der Titel darf nicht leer sein.")
    task = store.update(list_id, task_id, **fields)
    # Die Trenner-Zuordnung hängt am Listen-Record, nicht an der Erinnerung,
    # und kommt aus einem Einzelabruf nicht mit — aus dem Cache übernehmen.
    prev = next((t for t in cache.tasks(list_id) if t.id == task_id), None)
    if prev:
        task.section_id = prev.section_id
        task.section_name = prev.section_name
        task.section_order = prev.section_order
    cache.upsert_task(task)
    return task.as_dict()


@router.delete("/tasks/{list_id}/{task_id}", response_model=Ok, tags=["Aufgaben"],
               summary="Aufgabe löschen")
def delete_task(list_id: str, task_id: str):
    """iCloud kennt keinen Papierkorb — das ist endgültig."""
    store.delete(list_id, task_id)
    cache.drop_task(task_id)
    return Ok()


@router.post("/tasks/{list_id}/{task_id}/move", response_model=Task, tags=["Aufgaben"],
             summary="In eine andere Liste verschieben")
def move_task(list_id: str, task_id: str, body: MoveRequest):
    """Technisch Neuanlegen im Ziel plus Löschen im Original, weil CloudKit die
    Listenzugehörigkeit einer Erinnerung nicht ändern kann. Aufgaben mit Alarm,
    Tags, Anhang, Wiederholung oder Unteraufgaben werden mit 501 abgelehnt,
    statt diese Daten stillschweigend zu verlieren (siehe `movable`)."""
    task = store.move(list_id, task_id, body.target_list_id)
    cache.drop_task(task_id)
    cache.upsert_task(task)
    return task.as_dict()


@router.post("/tasks/{list_id}/{task_id}/section", response_model=Task, tags=["Aufgaben"],
             summary="Einem Trenner zuordnen")
def set_section(list_id: str, task_id: str, body: SectionRequest):
    """`section_id: null` löst die Zuordnung wieder auf."""
    task = store.set_section(list_id, task_id, body.section_id)
    cache.upsert_task(task)
    return task.as_dict()


# --------------------------------------------------------------------------
# Hilfsmittel
# --------------------------------------------------------------------------

@router.get("/parse", response_model=ParseResult, tags=["Hilfsmittel"],
            summary="Schnelleingabe auswerten, ohne etwas anzulegen")
def parse_text(text: str = Query("", description='z.B. "Zahnarzt anrufen morgen 14:30 !!"')):
    return quick_parse(text) if text.strip() else {"title": "", "due": None, "priority": 0}


@router.get("/sync", response_model=SyncStatus, tags=["Hilfsmittel"],
            summary="Wie frisch ist der Cache?")
def sync_status():
    return syncer.status()


@router.post("/sync", response_model=SyncStatus, tags=["Hilfsmittel"],
             summary="Vollen Sync erzwingen")
def force_sync():
    """Blockiert, bis der Durchlauf fertig ist (typisch einige Sekunden)."""
    syncer.full_sync()
    return syncer.status()
