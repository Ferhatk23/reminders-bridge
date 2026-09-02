"""Zugriff auf iCloud Erinnerungen ueber CloudKit (pyicloud).

Warum nicht CalDAV: dieser Account hat das Reminders-Upgrade (Apple HT210220)
mitgemacht. Apple hat die Erinnerungen dabei aus CalDAV in einen privaten
CloudKit-Speicher verschoben; ueber CalDAV kommt nur noch eine Stub-Liste
"Erinnerungen ⚠️" mit zwei Platzhaltern. CloudKit ist der einzige Weg, der die
echten Daten sieht.

Angemeldet wird nicht hier, sondern einmalig per scripts/todo/icloud-login.sh —
das legt eine vertrauenswuerdige Session unter $ICLOUD_SESSION_DIR ab, die laut
Apple rund zwei Monate haelt. Diese App liest die Session nur.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudAPIResponseException

from . import config

log = logging.getLogger("reminders.icloud")

SESSION_DIR = config.SESSION_DIR
LOCAL_TZ = config.LOCAL_TZ
# Kurzer Prozess-Cache. Die eigentliche Beschleunigung macht der SQLite-Cache
# davor; das hier verhindert nur, dass ein Sync-Durchlauf dieselbe Liste
# mehrfach abfragt.
CACHE_TTL = 30


class NotConfigured(RuntimeError):
    """Keine gueltige iCloud-Session vorhanden."""


class ICloudError(RuntimeError):
    """Fehler beim Reden mit iCloud, mit benutzbarer Meldung."""


class Unsupported(ICloudError):
    """Etwas, das die CloudKit-API nicht hergibt."""


# Apple: 1 = hoch, 5 = mittel, 9 = niedrig, 0/fehlend = keine.
PRIO_NONE, PRIO_HIGH, PRIO_MEDIUM, PRIO_LOW = 0, 1, 5, 9


def _prio_bucket(raw: int) -> int:
    if not raw:
        return PRIO_NONE
    if raw <= 4:
        return PRIO_HIGH
    if raw == 5:
        return PRIO_MEDIUM
    return PRIO_LOW


# CloudKit-IDs sehen aus wie "Reminder/<uuid>". Der Schraegstrich wuerde die
# Pfad-Routen der API zerlegen, deshalb nach aussen maskiert.
def enc(record_id: str) -> str:
    return record_id.replace("/", "~")


def dec(token: str) -> str:
    return token.replace("~", "/")


@dataclass
class Section:
    id: str
    name: str


@dataclass
class TaskList:
    id: str
    name: str
    color: str | None = None
    open_count: int = 0
    done_count: int = 0
    readonly: bool = False
    sections: list[dict] = field(default_factory=list)


@dataclass
class Task:
    id: str
    list_id: str
    list_name: str = ""
    title: str = ""
    notes: str = ""
    url: str = ""
    priority: int = PRIO_NONE
    due: str | None = None
    due_has_time: bool = False
    completed: bool = False
    completed_at: str | None = None
    flagged: bool = False
    recurring: bool = False
    linked: bool = False
    movable: bool = True
    block_reason: str = ""
    section_id: str = ""
    section_name: str = ""
    section_order: int = 9999

    @property
    def due_dt(self) -> datetime | None:
        if not self.due:
            return None
        try:
            if self.due_has_time:
                return datetime.fromisoformat(self.due).replace(tzinfo=LOCAL_TZ)
            d = date.fromisoformat(self.due)
            return datetime(d.year, d.month, d.day, 23, 59, tzinfo=LOCAL_TZ)
        except ValueError:
            return None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "list_id": self.list_id,
            "list_name": self.list_name,
            "title": self.title,
            "notes": self.notes,
            "url": self.url,
            "priority": self.priority,
            "due": self.due,
            "due_has_time": self.due_has_time,
            "completed": self.completed,
            "completed_at": self.completed_at,
            "flagged": self.flagged,
            "recurring": self.recurring,
            "linked": self.linked,
            "movable": self.movable,
            "block_reason": self.block_reason,
            "section_id": self.section_id,
            "section_name": self.section_name,
            "section_order": self.section_order,
        }
        dt = self.due_dt
        if dt:
            today = datetime.now(LOCAL_TZ).date()
            dd = dt.date()
            d["overdue"] = (not self.completed) and dd < today
            d["today"] = dd == today
            d["days"] = (dd - today).days
        else:
            d["overdue"] = d["today"] = False
            d["days"] = None
        return d


# --------------------------------------------------------------------------
# Zeitumrechnung
# --------------------------------------------------------------------------
# CloudKit liefert DueDate immer UTC-aware. Bei all_day=True steht dort 00:00
# UTC und das Datum ist woertlich gemeint - eine Umrechnung nach Europe/Berlin
# wuerde daraus faelschlich "02:00" machen. Getimte Erinnerungen fuehren
# zusaetzlich ein TimeZone-Feld und werden normal konvertiert.

def _due_to_iso(dt: datetime | None, all_day: bool, tz_name: str | None):
    if dt is None:
        return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if all_day:
        return dt.astimezone(timezone.utc).date().isoformat(), False
    tz = LOCAL_TZ
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            tz = LOCAL_TZ
    return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M"), True


def _iso_to_due(value: str | None):
    """-> (utc_datetime | None, all_day, time_zone_name | None)"""
    if not value:
        return None, False, None
    value = str(value).strip()
    if "T" in value:
        naive = datetime.fromisoformat(value)
        return naive.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc), False, config.TZ_NAME
    d = date.fromisoformat(value)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc), True, None


def _sort_tasks(tasks: list[Task]) -> list[Task]:
    prio_rank = {PRIO_HIGH: 0, PRIO_MEDIUM: 1, PRIO_LOW: 2, PRIO_NONE: 3}
    far = datetime(2999, 1, 1, tzinfo=LOCAL_TZ)

    def key(t: Task):
        return (t.completed, t.due_dt or far, prio_rank.get(t.priority, 3), t.title.lower())

    return sorted(tasks, key=key)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class Store:
    def __init__(self) -> None:
        # pyicloud haengt an einer requests.Session; alle Zugriffe serialisieren.
        self._lock = threading.RLock()
        self._api: PyiCloudService | None = None
        self._lists_cache: tuple[float, list[TaskList]] | None = None
        self._task_cache: dict[str, tuple[float, list[Task]]] = {}
        self._name_by_id: dict[str, str] = {}
        # Trenner ("Sections") aus iOS: Namen global, Reihenfolge + Asset-URL je Liste
        self._section_names: dict[str, str] = {}
        self._section_meta: dict[str, dict] = {}
        # Halbfertige Anmeldung zwischen Passwort- und 2FA-Schritt.
        self._pending: PyiCloudService | None = None

    @property
    def configured(self) -> bool:
        return bool(config.APPLE_ID) and os.path.isdir(SESSION_DIR) and bool(os.listdir(SESSION_DIR))

    def _svc(self):
        if not config.APPLE_ID:
            raise NotConfigured("ICLOUD_config.APPLE_ID ist nicht gesetzt.")
        with self._lock:
            if self._api is None:
                try:
                    api = PyiCloudService(config.APPLE_ID, cookie_directory=SESSION_DIR)
                except Exception as exc:  # noqa: BLE001
                    raise NotConfigured(_session_hint(exc)) from exc
                if api.requires_2fa or not api.is_trusted_session:
                    raise NotConfigured(
                        "Die iCloud-Session ist abgelaufen. Bitte neu anmelden "
                        "(POST /v1/session/login)."
                    )
                self._api = api
            return self._api.reminders

    # -- Anmeldung direkt aus der Weboberflaeche ---------------------------
    # Das Passwort wird nur fuer den einen Aufruf verwendet: es wird nirgends
    # gespeichert, nicht geloggt und nicht an die Oberflaeche zurueckgegeben.
    # Persistiert wird ausschliesslich die von Apple ausgestellte Session.

    def session_status(self) -> dict:
        if not config.APPLE_ID:
            return {"valid": False, "reason": "ICLOUD_config.APPLE_ID ist nicht gesetzt.",
                    "apple_id": "", "can_login": False}
        try:
            self._svc()
            return {"valid": True, "reason": "", "apple_id": config.APPLE_ID, "can_login": True}
        except NotConfigured as exc:
            return {"valid": False, "reason": str(exc), "apple_id": config.APPLE_ID,
                    "can_login": True}
        except ICloudError as exc:
            return {"valid": False, "reason": str(exc), "apple_id": config.APPLE_ID,
                    "can_login": True}

    def login(self, password: str) -> dict:
        """Schritt 1: Anmeldung mit dem echten Apple-ID-Passwort."""
        if not config.APPLE_ID:
            raise NotConfigured("ICLOUD_config.APPLE_ID ist nicht gesetzt.")
        if not password:
            raise ICloudError("Kein Passwort angegeben.")
        with self._lock:
            self._pending = None
            try:
                api = PyiCloudService(config.APPLE_ID, password, cookie_directory=SESSION_DIR)
            except Exception as exc:  # noqa: BLE001
                raise ICloudError(_login_hint(exc)) from exc

            if api.requires_2fa:
                self._pending = api
                return {"needs_2fa": True}
            if api.requires_2sa:
                raise ICloudError(
                    "Der Account nutzt die alte Zwei-Schritt-Bestaetigung. "
                    "Bitte in den Apple-ID-Einstellungen auf Zwei-Faktor umstellen."
                )
            if not api.is_trusted_session:
                api.trust_session()
            self._api = api
            self.invalidate()
            return {"needs_2fa": False}

    def submit_2fa(self, code: str) -> dict:
        """Schritt 2: den sechsstelligen Code vom Apple-Geraet bestaetigen."""
        with self._lock:
            api = self._pending
            if api is None:
                raise ICloudError(
                    "Keine Anmeldung offen — bitte mit dem Passwort neu beginnen."
                )
            code = (code or "").strip().replace(" ", "")
            if not code:
                raise ICloudError("Kein Code angegeben.")
            try:
                ok = api.validate_2fa_code(code)
            except Exception as exc:  # noqa: BLE001
                raise ICloudError(_friendly(exc)) from exc
            if not ok:
                raise ICloudError("Der Code wurde abgelehnt. Bitte neu eingeben.")
            if not api.is_trusted_session:
                try:
                    api.trust_session()
                except Exception as exc:  # noqa: BLE001
                    log.warning("trust_session fehlgeschlagen: %s", exc)
            self._api = api
            self._pending = None
            self.invalidate()
            return {"ok": True}

    def reset(self) -> None:
        with self._lock:
            self._api = None
            self._pending = None
            self.invalidate()

    def invalidate(self, list_id: str | None = None) -> None:
        with self._lock:
            self._lists_cache = None
            if list_id:
                self._task_cache.pop(list_id, None)
            else:
                self._task_cache.clear()

    # -- CloudKit-Eigenheiten ----------------------------------------------

    @staticmethod
    def _retry(fn: Callable, what: str = "Abfrage", tries: int = 4):
        """CloudKit antwortet beim Indexaufbau einer Zone mit TRY_AGAIN_LATER."""
        for attempt in range(1, tries + 1):
            try:
                return fn()
            except PyiCloudAPIResponseException as exc:
                text = str(exc)
                transient = "TRY_AGAIN_LATER" in text or "not valid" in text
                if not transient or attempt == tries:
                    raise ICloudError(_friendly(exc)) from exc
                wait = min(_retry_after(text), 20)
                log.info("CloudKit baut Index (%s), warte %ss", what, wait)
                time.sleep(wait)
            except (NotConfigured, ICloudError):
                raise
            except Exception as exc:  # noqa: BLE001
                raise ICloudError(_friendly(exc)) from exc
        raise ICloudError("CloudKit bleibt beschaeftigt.")

    # -- Listen -------------------------------------------------------------

    def _raw_lists(self) -> list[TaskList]:
        """Listen ueber die Rohdatensaetze lesen.

        pyicloud's record_to_list() wertet das Feld ``Deleted`` nicht aus, eine
        am iPhone geloeschte Liste taucht sonst als Geisterliste auf. Deshalb
        hier ueber die Rohdatensaetze, mit eigenem Deleted-Filter.
        """
        svc = self._svc()
        reads = svc._reads
        mapper = reads._mapper

        def go():
            out: list[TaskList] = []
            names: dict[str, str] = {}
            meta: dict[str, dict] = {}
            for zone in reads._iter_zone_change_pages(
                desired_record_types=["List", "ListSection"]
            ):
                for rec in zone.records:
                    rtype = getattr(rec, "recordType", None)
                    f = rec.fields
                    deleted = int(f.get_value("Deleted") or 0)

                    if rtype == "ListSection":
                        if deleted:
                            continue
                        # DisplayName kommt bei Sections als Klartext, nicht als
                        # CRDT-Dokument wie bei Titel/Notiz einer Erinnerung.
                        label = f.get_value("DisplayName") or f.get_value("CanonicalName")
                        if label:
                            names[_bare(rec.recordName)] = str(label)
                        continue

                    if rtype != "List" or deleted:
                        continue
                    model = mapper.record_to_list(rec)
                    if model.is_group:
                        continue
                    lid = enc(model.id)
                    meta[lid] = {
                        "order_url": _asset_url(f.get_value("SectionIDsOrderingAsData")),
                        "members_url": _asset_url(
                            f.get_value("MembershipsOfRemindersInSectionsAsData")
                        ),
                    }
                    out.append(
                        TaskList(
                            id=lid,
                            name=model.title,
                            color=_color_hex(f.get_value("Color")),
                        )
                    )
            with self._lock:
                self._section_names = names
                self._section_meta = meta
            return out

        return self._retry(go, "Listen")

    def _asset_json(self, url: str | None) -> Any:
        """Sections liegen nicht im Record, sondern in CloudKit-Assets — die
        sind aber schlichtes JSON, kein Protobuf."""
        if not url:
            return None
        import json

        try:
            raw = self._svc()._reads._get_raw().download_asset_bytes(url)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Section-Asset nicht lesbar: %s", exc)
            return None

    def _sections_for(self, list_id: str) -> list[dict]:
        meta = self._section_meta.get(list_id) or {}
        data = self._asset_json(meta.get("order_url")) or {}
        out = []
        for sid in data.get("orderedIdentifiers", []):
            name = self._section_names.get(sid)
            if name:
                out.append({"id": sid, "name": name})
        return out

    def _memberships(self, list_id: str) -> dict[str, str]:
        meta = self._section_meta.get(list_id) or {}
        data = self._asset_json(meta.get("members_url")) or {}
        out = {}
        for m in data.get("memberships", []):
            mid, gid = m.get("memberID"), m.get("groupID")
            if mid and gid:
                out[mid] = gid
        return out

    def lists(self) -> list[TaskList]:
        with self._lock:
            if self._lists_cache and time.time() - self._lists_cache[0] < CACHE_TTL:
                return self._lists_cache[1]

        lists = self._raw_lists()
        self._name_by_id = {l.id: l.name for l in lists}
        for l in lists:
            l.sections = self._sections_for(l.id)
            tasks = self._tasks_cached(l.id)
            l.open_count = sum(1 for t in tasks if not t.completed)
            l.done_count = sum(1 for t in tasks if t.completed)
        lists.sort(key=lambda l: l.name.lower())
        with self._lock:
            self._lists_cache = (time.time(), lists)
        return lists

    # -- Aufgaben -----------------------------------------------------------

    def _tasks_cached(self, list_id: str) -> list[Task]:
        with self._lock:
            hit = self._task_cache.get(list_id)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
        tasks = self._fetch_tasks(list_id)
        with self._lock:
            self._task_cache[list_id] = (time.time(), tasks)
        return tasks

    def _fetch_tasks(self, list_id: str) -> list[Task]:
        svc = self._svc()
        raw = dec(list_id)
        res = self._retry(
            lambda: svc.list_reminders(list_id=raw, include_completed=True, results_limit=200),
            self._name_by_id.get(list_id, list_id),
        )
        # Grabsteine: geloeschte Erinnerungen bleiben als Records liegen und
        # haengen weiter in ReminderIDs der Liste.
        reminders = [r for r in res.reminders if not r.deleted]
        parents = {r.parent_reminder_id for r in reminders if r.parent_reminder_id}
        name = self._name_by_id.get(list_id, "")

        members = self._memberships(list_id)
        order = {s["id"]: i for i, s in enumerate(self._sections_for(list_id))}
        names = self._section_names

        tasks = []
        for r in reminders:
            t = self._to_task(r, list_id, name, parents)
            sid = members.get(_bare(r.id))
            if sid and sid in order:
                t.section_id = sid
                t.section_name = names.get(sid, "")
                t.section_order = order[sid]
            tasks.append(t)
        return _sort_tasks(tasks)

    def _to_task(self, r, list_id: str, list_name: str, parents: set[str]) -> Task:
        due, has_time = _due_to_iso(r.due_date, r.all_day, r.time_zone)
        done_at, _ = _due_to_iso(r.completed_date, False, None)

        # Verschieben ist Loeschen+Neuanlegen; alles, was create() nicht
        # mitnehmen kann, wuerde dabei still verschwinden.
        blockers = []
        if r.alarm_ids:
            blockers.append("Erinnerungszeitpunkt")
        if r.hashtag_ids:
            blockers.append("Tags")
        if r.attachment_ids:
            blockers.append("Anhang")
        if r.recurrence_rule_ids:
            blockers.append("Wiederholung")
        if r.parent_reminder_id:
            blockers.append("Unteraufgabe")
        if _raw(r.id) in parents or r.id in parents:
            blockers.append("hat Unteraufgaben")

        return Task(
            id=enc(r.id),
            list_id=list_id,
            list_name=list_name,
            title=r.title or "",
            notes=r.desc or "",
            priority=_prio_bucket(int(r.priority or 0)),
            due=due,
            due_has_time=has_time,
            completed=bool(r.completed),
            completed_at=done_at,
            flagged=bool(r.flagged),
            recurring=bool(r.recurrence_rule_ids),
            linked=bool(r.parent_reminder_id) or (r.id in parents),
            movable=not blockers,
            block_reason=", ".join(blockers),
        )

    def tasks_in(self, list_id: str) -> list[Task]:
        self.lists()  # fuellt _name_by_id
        return self._tasks_cached(list_id)

    def all_tasks(self) -> list[Task]:
        out: list[Task] = []
        for l in self.lists():
            out.extend(self._tasks_cached(l.id))
        return _sort_tasks(out)

    # -- Delta-Sync ---------------------------------------------------------

    def sync_cursor(self) -> str:
        svc = self._svc()
        return self._retry(svc.sync_cursor, "Sync-Cursor")

    def changes_since(self, cursor: str) -> tuple[list[Task], list[str], str | None]:
        """Geaenderte/geloeschte Erinnerungen seit ``cursor``.

        Bewusst nicht ueber svc.iter_changes(): das liefert den neuen Cursor
        nicht zurueck, man muesste ihn separat nachladen - und in der Luecke
        zwischen beiden Aufrufen ginge eine Aenderung verloren. Hier wird der
        Token direkt aus derselben Antwort mitgenommen.
        """
        svc = self._svc()
        reads = svc._reads
        mapper = reads._mapper
        names = {l.id: l.name for l in self.lists()}

        def go():
            changed: list[Task] = []
            removed: list[str] = []
            token: str | None = None
            for zone in reads._iter_zone_change_pages(
                desired_record_types=["Reminder"], sync_token=cursor, reverse=False
            ):
                token = getattr(zone, "syncToken", None) or token
                for rec in zone.records:
                    rtype = getattr(rec, "recordType", None)
                    if rtype == "Reminder":
                        r = mapper.record_to_reminder(rec)
                        if r.deleted:
                            removed.append(enc(r.id))
                            continue
                        lid = enc(r.list_id)
                        if lid not in names:
                            # Liste geloescht oder eine Gruppe - beim naechsten
                            # vollen Sync faellt die Aufgabe ohnehin weg.
                            removed.append(enc(r.id))
                            continue
                        changed.append(self._to_task(r, lid, names[lid], set()))
                    elif getattr(rec, "recordName", None) and rtype is None:
                        removed.append(enc(rec.recordName))
            return changed, removed, token

        return self._retry(go, "Delta-Sync")

    # -- Schreiben ----------------------------------------------------------

    def _reminder(self, task_id: str):
        svc = self._svc()
        return self._retry(lambda: svc.get(dec(task_id)), "Aufgabe laden")

    def create(self, list_id: str, **f) -> Task:
        svc = self._svc()
        due, all_day, tz_name = _iso_to_due(f.get("due"))
        title = (f.get("title") or "Neue Erinnerung").strip()
        r = self._retry(
            lambda: svc.create(
                list_id=dec(list_id),
                title=title,
                desc=(f.get("notes") or "").strip(),
                due_date=due,
                priority=_prio_bucket(int(f.get("priority") or 0)),
                flagged=bool(f.get("flagged")),
                all_day=all_day,
                time_zone=tz_name,
            ),
            "Anlegen",
        )
        self._task_cache.pop(list_id, None)
        return self._to_task(r, list_id, self._name_by_id.get(list_id, ""), set())

    def update(self, list_id: str, task_id: str, **f) -> Task:
        svc = self._svc()
        r = self._reminder(task_id)

        if "title" in f:
            r.title = str(f["title"]).strip()
        if "notes" in f:
            r.desc = f["notes"] or ""
        if "priority" in f:
            r.priority = _prio_bucket(int(f["priority"] or 0))
        if "flagged" in f:
            r.flagged = bool(f["flagged"])
        if "due" in f:
            due, all_day, tz_name = _iso_to_due(f["due"])
            r.due_date = due
            r.all_day = all_day
            r.time_zone = tz_name
        if "completed" in f:
            r.completed = bool(f["completed"])
            if not r.completed:
                r.completed_date = None

        self._retry(lambda: svc.update(r), "Speichern")
        self._task_cache.pop(list_id, None)
        fresh = self._reminder(task_id)
        return self._to_task(fresh, list_id, self._name_by_id.get(list_id, ""), set())

    def delete(self, list_id: str, task_id: str) -> None:
        svc = self._svc()
        r = self._reminder(task_id)
        self._retry(lambda: svc.delete(r), "Loeschen")
        self.invalidate(list_id)

    def move(self, list_id: str, task_id: str, target_list_id: str) -> Task:
        """CloudKit-update() kann die Zugehoerigkeit zur Liste nicht aendern.

        Bleibt Neuanlegen im Ziel + Loeschen im Original. Alles, was create()
        nicht abbildet (Alarme, Tags, Anhaenge, Wiederholung, Unteraufgaben),
        ginge dabei verloren - solche Erinnerungen werden deshalb abgelehnt
        statt stillschweigend beschnitten.
        """
        if target_list_id == list_id:
            raise ICloudError("Quelle und Ziel sind dieselbe Liste.")

        current = next((t for t in self.tasks_in(list_id) if t.id == task_id), None)
        if current is None:
            raise ICloudError("Aufgabe nicht gefunden.")
        if not current.movable:
            raise Unsupported(
                f"„{current.title}“ laesst sich hier nicht verschieben: "
                f"{current.block_reason} ginge dabei verloren. Bitte am iPhone verschieben."
            )

        svc = self._svc()
        r = self._reminder(task_id)
        due, all_day, tz_name = _iso_to_due(current.due)
        created = self._retry(
            lambda: svc.create(
                list_id=dec(target_list_id),
                title=r.title,
                desc=r.desc or "",
                completed=bool(r.completed),
                due_date=due,
                priority=int(r.priority or 0),
                flagged=bool(r.flagged),
                all_day=all_day,
                time_zone=tz_name,
            ),
            "Verschieben",
        )
        try:
            self._retry(lambda: svc.delete(r), "Original loeschen")
        except ICloudError as exc:
            self.invalidate()
            raise ICloudError(
                "Die Kopie liegt in der Zielliste, das Original liess sich aber "
                f"nicht loeschen: {exc}"
            ) from exc

        self.invalidate(list_id)
        self.invalidate(target_list_id)
        self.lists()
        return self._to_task(created, target_list_id, self._name_by_id.get(target_list_id, ""), set())

    # -- Trenner (Sections) -------------------------------------------------
    # Die Zuordnung Erinnerung->Trenner steht nicht am Reminder, sondern in
    # einem CloudKit-Asset am Listen-Record: einer JSON-Datei mit einem Eintrag
    # je Erinnerung. Schreiben heisst deshalb: Datei holen, den einen Eintrag
    # aendern, neu hochladen, Listen-Record aktualisieren.
    #
    # Entschaerfend: jeder Eintrag traegt sein eigenes ``modifiedOn``. Das ist
    # ein Last-Writer-Wins pro Erinnerung, kein monolithischer Block - ein
    # Schreibvorgang kann also nicht die Zuordnung der uebrigen Erinnerungen
    # umwerfen, solange man deren Eintraege unveraendert durchreicht.
    #
    # Der eigentliche Knackpunkt ist NICHT die Pruefsumme, sondern die
    # ``ResolutionTokenMap``: Apples remindd fuehrt pro Feld eine CRDT-Vektoruhr
    # aus counter/modificationTime/replicaID. Schreibt man dort einen counter,
    # der unter dem liegt, den das Geraet lokal fuehrt, nimmt der Server die
    # Daten zwar an — das iPhone verwirft sie aber bei der Konfliktaufloesung
    # und zeigt die Erinnerung weiter ausserhalb der Trenner. pyicloud's
    # _generate_resolution_token_map setzt counter *immer* auf 1 und ist hier
    # deshalb unbrauchbar.
    #
    # Zwei Regeln daraus:
    #   1. Die bestehende Map einlesen und nur die eigenen Felder anfassen —
    #      sie zu ersetzen loescht die Uhren aller anderen Felder.
    #   2. Den counter ueber das Maximum der gesamten Map heben, damit er auch
    #      dann gewinnt, wenn das Geraet fuer dieses Feld schon weiter ist.
    #
    # Apples ``…Checksum``-Feld ist nicht rekonstruierbar (128 Hex, weder
    # SHA-256/384/512 noch BLAKE2b/SHA3 ueber Inhalt, Kanonisierungen oder
    # XOR/Summe von Einzel-Hashes). Wir schreiben dort einen SHA-512 des neuen
    # Inhalts — empirisch stoert das nicht, sobald die Vektoruhr stimmt.

    APPLE_EPOCH = config.APPLE_EPOCH
    _MEMBERS_FIELD = "MembershipsOfRemindersInSectionsAsData"
    _MEMBERS_CHECKSUM = "MembershipsOfRemindersInSectionsChecksum"
    # Schluessel in der ResolutionTokenMap sind camelCase, nicht die Feldnamen.
    _TOKEN_KEYS = (
        "membershipsOfRemindersInSectionsAsData",
        "membershipsOfRemindersInSectionsChecksum",
    )

    def _replica_id(self) -> str:
        """Stabile Replik-Kennung dieser Installation.

        remindd identifiziert Sync-Quellen ueber diese UUID; sie sollte
        zwischen Schreibvorgaengen gleich bleiben, nicht je Aufruf neu sein.
        """
        with self._lock:
            if not getattr(self, "_replica", None):
                import uuid as _uuid

                path = os.path.join(SESSION_DIR, "replica-id")
                try:
                    with open(path) as fh:
                        self._replica = fh.read().strip()
                except OSError:
                    self._replica = str(_uuid.uuid4()).upper()
                    try:
                        with open(path, "w") as fh:
                            fh.write(self._replica)
                    except OSError:
                        pass
            return self._replica

    def _bumped_token_map(self, rec) -> str:
        import json as _json

        raw_map = rec.fields.get_value("ResolutionTokenMap")
        if isinstance(raw_map, bytes):
            raw_map = raw_map.decode("utf-8", "replace")
        try:
            doc = _json.loads(raw_map) if raw_map else {}
        except ValueError:
            doc = {}
        tokens = doc.get("map") if isinstance(doc.get("map"), dict) else {}

        highest = 0
        for entry in tokens.values():
            try:
                highest = max(highest, int(entry.get("counter", 0)))
            except (TypeError, ValueError, AttributeError):
                pass

        stamp = time.time() - self.APPLE_EPOCH
        replica = self._replica_id()
        for key in self._TOKEN_KEYS:
            tokens[key] = {
                "counter": highest + 1,
                "modificationTime": stamp,
                "replicaID": replica,
            }
        doc["map"] = tokens
        return _json.dumps(doc, separators=(",", ":"))

    def _list_record(self, list_id: str):
        reads = self._svc()._reads
        want = dec(list_id)
        for zone in reads._iter_zone_change_pages(desired_record_types=["List"]):
            for rec in zone.records:
                if getattr(rec, "recordName", None) == want:
                    return rec
        raise ICloudError("Liste nicht gefunden.")

    def set_section(self, list_id: str, task_id: str, section_id: str | None) -> Task:
        import hashlib
        import json as _json
        import time as _time

        svc = self._svc()
        reads = svc._reads
        writes = svc._writes
        raw = reads._get_raw()
        http = raw._client._http

        valid = {s["id"] for s in self._sections_for(list_id)}
        if section_id and section_id not in valid:
            raise ICloudError("Diesen Trenner gibt es in der Liste nicht.")

        def go():
            rec = self._list_record(list_id)
            url = _asset_url(rec.fields.get_value(self._MEMBERS_FIELD))
            doc = {"minimumSupportedVersion": 20230430, "memberships": []}
            if url:
                loaded = self._asset_json(url)
                if isinstance(loaded, dict) and "memberships" in loaded:
                    doc = loaded

            member = _bare(dec(task_id))
            stamp = round(_time.time() - self.APPLE_EPOCH, 6)
            entries = [m for m in doc.get("memberships", []) if m.get("memberID") != member]
            entry = {"memberID": member, "modifiedOn": stamp}
            if section_id:
                entry["groupID"] = section_id
            entries.append(entry)
            doc["memberships"] = entries

            body = _json.dumps(doc, ensure_ascii=False).encode("utf-8")

            tokens = http.post("/assets/upload", {"tokens": [{
                "recordName": rec.recordName,
                "recordType": "List",
                "fieldName": self._MEMBERS_FIELD,
            }]})
            up = http._session.post(
                tokens["tokens"][0]["url"], data=body,
                headers={"Content-Type": "text/plain"}, timeout=60,
            )
            up.raise_for_status()
            receipt = up.json()["singleFile"]

            class _Shim:
                record_change_tag = rec.recordChangeTag

            writes._submit_single_record_update(
                operation_name="Trenner zuordnen",
                record_name=rec.recordName,
                record_type="List",
                record_change_tag=rec.recordChangeTag,
                fields={
                    self._MEMBERS_FIELD: {"type": "ASSETID", "value": receipt},
                    self._MEMBERS_CHECKSUM: {
                        "type": "STRING",
                        "value": hashlib.sha512(body).hexdigest(),
                        "isEncrypted": True,
                    },
                    "ResolutionTokenMap": {
                        "type": "STRING",
                        "value": self._bumped_token_map(rec),
                    },
                },
                model_obj=_Shim(),
            )

        self._retry(go, "Trenner zuordnen")
        self.invalidate(list_id)
        self.lists()
        return next(t for t in self.tasks_in(list_id) if t.id == task_id)

    # -- Listenverwaltung: von der CloudKit-API nicht angeboten --------------

    _NO_LISTS = (
        "Listen anlegen, umbenennen und loeschen geht ueber Apples CloudKit-API "
        "nicht - das bitte am iPhone erledigen. Hier sind Listen lesbar und als "
        "Verschiebe-Ziel waehlbar."
    )

    def create_list(self, name: str):
        raise Unsupported(self._NO_LISTS)

    def rename_list(self, list_id: str, name: str) -> None:
        raise Unsupported(self._NO_LISTS)

    def delete_list(self, list_id: str) -> None:
        raise Unsupported(self._NO_LISTS)


# --------------------------------------------------------------------------

def _raw(x: str) -> str:
    return dec(x) if "~" in x else x


def _bare(record_id: str) -> str:
    """"Reminder/<uuid>" -> "<uuid>" — Sections referenzieren nackte UUIDs."""
    return record_id.rsplit("/", 1)[-1]


def _asset_url(value) -> str | None:
    """CloudKit-Assetfelder tragen die downloadURL als Text mit."""
    if not value:
        return None
    import re

    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    m = re.search(r"downloadURL='([^']+)'", text)
    return m.group(1) if m else None


def _retry_after(text: str) -> int:
    import json
    import re

    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return int(json.loads(m.group(0)).get("retryAfter", 10))
        except Exception:  # noqa: BLE001
            pass
    return 10


def _color_hex(value) -> str | None:
    """Apple liefert Farben als JSON-Blob mit daHexString."""
    if not value:
        return None
    if isinstance(value, str):
        if value.startswith("#"):
            return value[:7]
        try:
            import json

            value = json.loads(value)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, dict):
        hexs = value.get("daHexString")
        if isinstance(hexs, str) and hexs.startswith("#"):
            return hexs[:7]
    return None


def _login_hint(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "invalid" in low or "401" in msg or "password" in low:
        return "Apple hat Benutzername oder Passwort abgelehnt."
    if "locked" in low or "disabled" in low:
        return "Der Apple-Account ist gesperrt — bitte auf appleid.apple.com pruefen."
    if "too many" in low or "429" in msg:
        return "Zu viele Anmeldeversuche. Apple bremst kurzzeitig, spaeter erneut versuchen."
    return _friendly(exc)


def _session_hint(exc: Exception) -> str:
    return (
        "Keine gueltige iCloud-Session. Bitte neu anmelden "
        f"(POST /v1/session/login). ({_friendly(exc)})"
    )


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "401" in msg or "unauthorized" in low or "authentication" in low:
        return "iCloud hat die Session abgelehnt - bitte neu anmelden."
    if "503" in msg or "maintenance" in low:
        return "iCloud meldet Wartung."
    if "try_again_later" in low:
        return "CloudKit baut gerade seinen Index auf - gleich nochmal versuchen."
    if "timed out" in low or "timeout" in low:
        return "iCloud hat nicht rechtzeitig geantwortet."
    return msg.split("\n")[0][:400] or exc.__class__.__name__


store = Store()
