"""Lokaler SQLite-Lesecache.

Die CloudKit-Abfragen kosten pro vollem Durchlauf rund 5-6 Sekunden; das ist
fuer eine Oberflaeche, die man nebenbei benutzt, zu langsam. Deshalb liest die
App ausschliesslich aus dieser Datei und ein Hintergrund-Thread haelt sie
aktuell (siehe app/sync.py).

Wichtig fuer die Einordnung: das hier ist ein *Cache*, keine zweite Wahrheit.
Geschrieben wird immer zuerst nach iCloud; erst der Erfolg landet hier. Damit
kann nichts auseinanderlaufen, was ein echter Zwei-Wege-Sync riskieren wuerde -
schlimmstenfalls ist der Cache kurz veraltet.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

from .icloud import Task

from . import config

DB_PATH = config.CACHE_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    color    TEXT,
    sections TEXT NOT NULL DEFAULT '[]',
    pos      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    id      TEXT PRIMARY KEY,
    list_id TEXT NOT NULL,
    data    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_list ON tasks(list_id);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Cache:
    def __init__(self, path: str = DB_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- Meta ---------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, value),
            )
            self._db.commit()

    @property
    def empty(self) -> bool:
        with self._lock:
            return not self._db.execute("SELECT 1 FROM lists LIMIT 1").fetchone()

    # -- Lesen --------------------------------------------------------------

    def lists(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM lists ORDER BY pos, name").fetchall()
            counts = {}
            for r in self._db.execute(
                "SELECT list_id, data FROM tasks"
            ).fetchall():
                d = json.loads(r["data"])
                o, c = counts.get(r["list_id"], (0, 0))
                counts[r["list_id"]] = (o + (0 if d["completed"] else 1),
                                        c + (1 if d["completed"] else 0))
        out = []
        for r in rows:
            o, c = counts.get(r["id"], (0, 0))
            out.append({
                "id": r["id"], "name": r["name"], "color": r["color"],
                "sections": json.loads(r["sections"]),
                "open_count": o, "done_count": c, "readonly": False,
            })
        return out

    def list_name(self, list_id: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT name FROM lists WHERE id=?", (list_id,)).fetchone()
        return row["name"] if row else ""

    def tasks(self, list_id: str | None = None) -> list[Task]:
        with self._lock:
            if list_id:
                rows = self._db.execute(
                    "SELECT data FROM tasks WHERE list_id=?", (list_id,)).fetchall()
            else:
                rows = self._db.execute("SELECT data FROM tasks").fetchall()
        return [Task(**json.loads(r["data"])) for r in rows]

    # -- Schreiben ----------------------------------------------------------

    def replace_lists(self, lists: list) -> None:
        with self._lock:
            self._db.execute("DELETE FROM lists")
            self._db.executemany(
                "INSERT INTO lists(id,name,color,sections,pos) VALUES(?,?,?,?,?)",
                [(l.id, l.name, l.color, json.dumps(l.sections), i)
                 for i, l in enumerate(lists)],
            )
            # Aufgaben verwaister Listen (am iPhone geloescht) mitnehmen.
            self._db.execute(
                "DELETE FROM tasks WHERE list_id NOT IN (SELECT id FROM lists)")
            self._db.commit()

    def replace_tasks(self, list_id: str, tasks: list[Task]) -> None:
        with self._lock:
            self._db.execute("DELETE FROM tasks WHERE list_id=?", (list_id,))
            self._db.executemany(
                "INSERT INTO tasks(id,list_id,data) VALUES(?,?,?)",
                [(t.id, list_id, json.dumps(t.__dict__)) for t in tasks],
            )
            self._db.commit()

    def upsert_task(self, task: Task) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO tasks(id,list_id,data) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET list_id=excluded.list_id, data=excluded.data",
                (task.id, task.list_id, json.dumps(task.__dict__)),
            )
            self._db.commit()

    def drop_task(self, task_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._db.commit()


cache = Cache()
