# Reminders Bridge

A REST API and web editor for **Apple Reminders**, running on Linux. No Mac, no
iPhone shortcut, no CalDAV.

Built because iCloud Reminders became unreachable for third-party tools: once an
account takes the *Reminders upgrade* (iOS 13 / macOS Catalina, [HT210220]),
Apple moves the data out of CalDAV into a private CloudKit store. A CalDAV
client then sees exactly one stub list — `Reminders ⚠️` — containing two
placeholders titled *"Where are my reminders?"*. The upgrade cannot be undone.

This project talks to that CloudKit store instead, through [pyicloud]. It reads
and writes reminders, and — as far as I can tell, uniquely — it can also move
reminders **between sections** from outside an Apple device.

[HT210220]: https://support.apple.com/HT210220
[pyicloud]: https://github.com/picklepete/pyicloud

---

## What it can do

| | |
|---|---|
| Reminders | create, edit, complete, delete, move between lists |
| Fields | title, notes, due date (all-day or timed), priority, flag |
| Sections | read, group by, and **assign reminders to them** |
| Lists | read, with per-list section order and counts |
| Recurrence / subtasks | shown and preserved, not editable |

**Not possible** (Apple's CloudKit API offers no operations for it): creating,
renaming or deleting lists and sections. Do that on an Apple device; they show
up here on the next sync.

## Quick start

```bash
git clone https://github.com/tobiasredel/reminders-bridge.git
cd reminders-bridge
cp .env.example .env      # set ICLOUD_USERNAME, ideally API_TOKEN
docker compose up -d api
```

Open <http://localhost:8000>, sign in with your **real** Apple ID password plus
the six-digit 2FA code. An app-specific password does **not** work here —
CloudKit rejects them. The session is then stored in `data/session` and lasts
about two months.

Same thing without a browser:

```bash
curl -X POST localhost:8000/v1/session/login \
     -H 'Content-Type: application/json' -d '{"password":"…"}'
curl -X POST localhost:8000/v1/session/code \
     -H 'Content-Type: application/json' -d '{"code":"123456"}'
```

Interactive API docs: <http://localhost:8000/docs>

## Layout

The API is the product; the web UI is one of its clients.

```
api/reminders_api/     the service
    icloud.py          CloudKit access — the interesting part
    cache.py           SQLite read cache
    sync.py            background sync (delta + full)
    quickadd.py        German date/priority parser
    routes.py          HTTP surface (/v1)
web/public/            web UI — static files, no build step
```

Run them together or apart:

```bash
docker compose up -d api              # one container, API serves the UI too
docker compose --profile split up -d  # separate: nginx serves the UI,
                                      # proxies /v1, and injects the API token
                                      # so the browser never sees it
```

For a UI on a different origin, set `REMINDERS_API_BASE` in
`web/public/config.js` and add that origin to `CORS_ORIGINS`.

## How it works

**Reads come from a local SQLite cache, writes go straight to iCloud.**

A full CloudKit pass costs about 5–6 seconds, which is far too slow for a UI you
use casually. So a background thread keeps a SQLite mirror current and every
read is served from it in a few milliseconds. Writes are never cached: they go
to iCloud synchronously and only the result is written back. iCloud stays the
single source of truth — there is no two-way sync and therefore nothing that can
diverge. The worst case is a cache that is briefly stale.

The background thread runs two kinds of pass:

* **delta** every 45 s (~0.3 s) — only reminders changed since the last sync
  cursor
* **full** every 5 min (~5.7 s) — also catches renamed lists, new lists and
  changed sections

Both only run while the API is actually being used (`SYNC_ACTIVE_WINDOW`), so an
idle instance does not poll Apple around the clock.

### Quick add

`POST /v1/tasks` parses German dates and priorities out of the title by default:

| input | result |
|---|---|
| `Zahnarzt anrufen morgen 14:30` | tomorrow, 14:30 |
| `Steuer !!! 15.10.` | 15 Oct, high priority |
| `Bericht freitag` | next Friday |
| `Rechnung in 3 tagen` | today + 3 |

`!` low · `!!` medium · `!!!` high. Two-letter weekday abbreviations only count
after `am`/`nächsten`, otherwise the parser would eat ordinary words. Send
`parse: false` to disable, or preview with `GET /v1/parse?text=…`.

The web UI and this parser are **German**. The API itself is language-neutral;
a parser for another language is a single self-contained module.

## Configuration

| variable | default | meaning |
|---|---|---|
| `ICLOUD_USERNAME` | — | Apple ID, required |
| `API_TOKEN` | *(empty)* | if set, `Authorization: Bearer …` is required |
| `CORS_ORIGINS` | *(empty)* | comma-separated, for a UI on another origin |
| `SERVE_WEB` | `true` | let the API serve the web UI as well |
| `SYNC_DELTA_SECONDS` | `45` | delta sync interval |
| `SYNC_FULL_SECONDS` | `300` | full sync interval |
| `SYNC_ACTIVE_WINDOW` | `600` | stop syncing this long after the last request |
| `TZ` | `Europe/Berlin` | used for all-day vs. timed due dates |
| `ICLOUD_SESSION_DIR` | `/session` | iCloud session, keep this |
| `CACHE_DB` | `/data/cache.db` | read cache, safe to delete |

An empty `API_TOKEN` leaves the API **open**. That is fine behind an
authenticating reverse proxy and a bad idea otherwise.

## Security

Your Apple ID password is passed through once to obtain a session. It is never
stored and never logged. What is stored is the session Apple issues, in
`data/session` — treat that directory like a password: it grants access to your
iCloud reminders until it expires.

## Limitations

* **Deleting is final.** iCloud has no trash for reminders.
* **Moving between lists** is create-then-delete, because CloudKit cannot change
  a reminder's list. Reminders carrying alarms, tags, attachments, recurrence or
  subtasks are refused for that operation (`movable: false`, with
  `block_reason`) instead of silently losing those. Section changes are a plain
  field update and are always allowed.
* **No URL field.** CloudKit reminders don't have one; URLs are separate
  attachment records.
* Lists and sections themselves are read-only, see above.

## How this compares

[remi] and [remctl] both support sections with iCloud sync, and both are
**macOS-only**: they go through the private ReminderKit framework or the local
SQLite database and let Apple's own `remindd` daemon do the CloudKit push.
EventKit, Apple's public API, has no section support at all.
[apple-reminders-mcp-cli] runs on Linux but writes through an iCloud Drive file
picked up by an iPhone shortcut.

This project writes to CloudKit directly. Getting section membership to actually
arrive on the phone took four failed attempts — the reason, and everything else
worth knowing about the format, is in **[docs/INTERNALS.md](docs/INTERNALS.md)**.

[remi]: https://github.com/mattheworiordan/remi
[remctl]: https://github.com/viticci/remctl
[apple-reminders-mcp-cli]: https://github.com/jkan67-de/apple-reminders-mcp-cli

## Credits

Built on [pyicloud], whose CloudKit reminders implementation does the heavy
lifting. The section internals were pieced together with help from the
reverse-engineering notes in [remi]'s `APPLE_REMINDERS_INTERNALS.md`.

Not affiliated with Apple. Uses undocumented, unsupported interfaces that may
break at any time.

## License

MIT — see [LICENSE](LICENSE).
