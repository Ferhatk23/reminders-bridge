"""Hintergrund-Sync zwischen iCloud und dem lokalen Lesecache.

Zwei Stufen, weil die Kosten weit auseinanderliegen (gemessen an echten Daten):

  * **voll**  ~5,7 s — Listen + Trenner + alle Erinnerungen. Noetig, um
    Umbenennungen, neue/geloeschte Listen und geaenderte Trenner mitzubekommen.
  * **Delta** ~0,3 s — nur geaenderte Erinnerungen seit dem letzten Sync-Cursor.

Gesynct wird nur, solange die Seite tatsaechlich benutzt wird (letzter Request
innerhalb von ACTIVE_WINDOW). Sonst laege hier ein Prozess, der rund um die Uhr
alle 45 Sekunden Apple anfunkt, ohne dass jemand hinschaut.
"""
from __future__ import annotations

import logging
import threading
import time

from .cache import cache
from .icloud import ICloudError, NotConfigured, store

log = logging.getLogger("reminders.sync")

from . import config

DELTA_INTERVAL = config.SYNC_DELTA_SECONDS
FULL_INTERVAL = config.SYNC_FULL_SECONDS
ACTIVE_WINDOW = config.SYNC_ACTIVE_WINDOW


class Syncer:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._force_full = False
        self._last_touch = 0.0
        self._last_full = 0.0
        self._last_delta = 0.0
        self._last_error = ""
        self._syncing = False
        self._thread: threading.Thread | None = None

    # -- Zustand ------------------------------------------------------------

    def touch(self) -> None:
        """Merken, dass die Seite gerade benutzt wird."""
        self._last_touch = time.time()

    @property
    def active(self) -> bool:
        return time.time() - self._last_touch < ACTIVE_WINDOW

    def status(self) -> dict:
        return {
            "syncing": self._syncing,
            "last_sync": self._last_delta or self._last_full or 0,
            "age": int(time.time() - (self._last_delta or self._last_full)) 
                   if (self._last_delta or self._last_full) else None,
            "error": self._last_error,
        }

    def request_full(self) -> None:
        with self._lock:
            self._force_full = True
        self._wake.set()

    # -- Ablauf -------------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="sync", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("Sync-Durchlauf fehlgeschlagen: %s", exc)
                self._last_error = str(exc)
            self._wake.wait(timeout=15)
            self._wake.clear()

    def _tick(self) -> None:
        with self._lock:
            forced = self._force_full
            self._force_full = False
        now = time.time()

        if not forced and not self.active:
            return
        if forced or cache.empty or now - self._last_full > FULL_INTERVAL:
            self.full_sync()
        elif now - self._last_delta > DELTA_INTERVAL:
            self.delta_sync()

    # -- Die eigentliche Arbeit ---------------------------------------------

    def full_sync(self) -> None:
        with self._lock:
            self._syncing = True
        t0 = time.time()
        try:
            store.invalidate()
            lists = store.lists()
            cache.replace_lists(lists)
            for l in lists:
                cache.replace_tasks(l.id, store.tasks_in(l.id))
            try:
                cache.set_meta("cursor", store.sync_cursor())
            except Exception as exc:  # noqa: BLE001
                log.debug("Sync-Cursor nicht verfuegbar: %s", exc)
            self._last_full = self._last_delta = time.time()
            self._last_error = ""
            log.info("Voller Sync: %d Listen in %.1fs", len(lists), time.time() - t0)
        except (NotConfigured, ICloudError) as exc:
            self._last_error = str(exc)
            raise
        finally:
            self._syncing = False

    def delta_sync(self) -> None:
        cursor = cache.get_meta("cursor")
        if not cursor:
            self.full_sync()
            return
        with self._lock:
            self._syncing = True
        t0 = time.time()
        try:
            changed, removed, cursor = store.changes_since(cursor)
            for task in changed:
                cache.upsert_task(task)
            for task_id in removed:
                cache.drop_task(task_id)
            if cursor:
                cache.set_meta("cursor", cursor)
            self._last_delta = time.time()
            self._last_error = ""
            if changed or removed:
                log.info("Delta-Sync: %d geaendert, %d entfernt (%.2fs)",
                         len(changed), len(removed), time.time() - t0)
        except (NotConfigured, ICloudError) as exc:
            self._last_error = str(exc)
            raise
        finally:
            self._syncing = False


syncer = Syncer()
