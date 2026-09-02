# How Apple Reminders looks from the outside

Notes from building this. Everything here was found empirically against a live
iCloud account; none of it is documented by Apple, and all of it may change.

## CalDAV is a dead end

iCloud still speaks CalDAV, and reminder lists used to be CalDAV calendars whose
`supported-calendar-component-set` contains `VTODO`. Once an account takes the
Reminders upgrade ([HT210220]), Apple migrates the data into a private CloudKit
store and leaves behind a single stub list, usually named `Reminders ⚠️`, holding
two placeholder items:

```
Where are my reminders?
The creator of this list upgraded these reminders.
```

The ⚠️ in the list name is Apple's marker. The upgrade is one-way. If you see
that stub, stop writing a CalDAV client.

**Check before you build.** Listing the lists takes a minute and tells you
immediately whether real data is reachable.

[HT210220]: https://support.apple.com/HT210220

## The CloudKit zone

Everything lives in one CloudKit zone. Record types observed:

| type | note |
|---|---|
| `Reminder` | the reminders themselves |
| `List` | a reminder list |
| `ListSection` | a section ("divider") inside a list |
| `Alarm`, `AlarmTrigger` | time and location alarms |
| `Account`, `cloudkit.share` | plumbing |

`pyicloud` ≥ 2.6.5 models `Reminder` and `List` with full CRUD. Note that
GitHub `master` of pyicloud still shows the old `/rd/` web API with read+create
only — **look at the released package, not the repo**.

### Record IDs contain slashes

`Reminder/<uuid>`, `List/<uuid>`. That breaks naive URL path routing; this
project escapes `/` as `~` on the way out.

### Fields are encrypted

Writing `{"type": "STRING"}` to an `ENCRYPTED_STRING` field is rejected with

```
invalid attempt to set value type ENCRYPTED_BYTES for field '…',
defined to be: ENCRYPTED_STRING
```

The correct shape is `{"type": "STRING", "value": …, "isEncrypted": true}`.

### Timestamps

Two different epochs are in play:

* CloudKit `TIMESTAMP` fields decode to **UTC-aware** datetimes.
* Timestamps *inside* the JSON blobs use the **Apple epoch**: seconds since
  2001-01-01, i.e. Unix time minus `978307200`.

**All-day reminders are stored as 00:00 UTC** with `all_day = true` and no
`time_zone`. Converting those to a local zone displays "02:00" in Europe and can
shift the date across a DST boundary. Only timed reminders — which carry a
`time_zone` field — should be converted.

### Tombstones

Deleted reminders remain as records with `deleted = 1`, and stay referenced in
the list's `ReminderIDs`. Consequences:

* Filter `deleted` yourself when reading.
* `RemindersList.count` counts tombstones. A list that had been deleted reported
  `count = 7` with zero live reminders.
* pyicloud's `record_to_list()` does **not** read the `Deleted` field, so a list
  deleted on the phone keeps showing up. Read the raw records and filter
  yourself if that matters.

### First query of a zone

CloudKit answers the first query against a zone with

```json
{"serverErrorCode": "TRY_AGAIN_LATER", "retryAfter": 30,
 "reason": "These index(es) is/are not valid; Indexing scheduled…"}
```

while it builds the query index. Without a retry this looks like a hard failure.

## Sections

This is the part nothing else seems to do from outside an Apple device.

### Where the data lives

A section is a `ListSection` record. Its `DisplayName` is **plain text**, not a
CRDT document — unlike a reminder's title and notes, which are.

The membership — which reminder sits in which section — is **not** on the
reminder. `Reminder` records have no section field at all. It lives on the
`List` record, in two **CloudKit assets**:

* `SectionIDsOrderingAsData` — the sections of this list, in display order
* `MembershipsOfRemindersInSectionsAsData` — the actual mapping

The record fields only carry a `downloadURL`; you have to fetch the asset. The
payloads are plain JSON:

```json
{"orderedIdentifiers": ["AAAAAAAA-…", "BBBBBBBB-…"], "minimumSupportedVersion": 20230430}
```

```json
{"minimumSupportedVersion": 20230430,
 "memberships": [
   {"memberID": "11111111-…", "groupID": "AAAAAAAA-…", "modifiedOn": 810022299.537718},
   {"memberID": "22222222-…", "modifiedOn": 810022214.645717}
 ]}
```

`memberID` and `groupID` are **bare UUIDs**, without the `Reminder/` or
`ListSection/` prefix. A missing `groupID` means "no section" — iOS shows those
under *Other*. Key order within an entry varies and is irrelevant.

Each entry carries its own `modifiedOn`. That makes this a per-reminder
last-writer-wins structure rather than one monolithic value, which is what makes
writing it reasonably safe: pass the other entries through untouched and you
cannot disturb them.

### Writing an asset

`pyicloud` can only download assets, but the upload endpoint is reachable
through its generic HTTP client:

1. `POST /assets/upload` with
   `{"tokens": [{"recordName": "List/…", "recordType": "List", "fieldName": "MembershipsOfRemindersInSectionsAsData"}]}`
   → returns a one-shot upload URL.
2. `POST` the raw bytes to that URL → returns a `singleFile` receipt containing
   `receipt`, `size`, `fileChecksum`, `referenceChecksum`, `wrappingKey`.
3. Include the receipt in a record update as
   `{"type": "ASSETID", "value": <receipt>}`.

The stored bytes are the plaintext JSON — uploading the exact bytes you
downloaded reproduces the same `fileChecksum`.

### The trap: `ResolutionTokenMap`

This is what actually decides whether your change reaches the phone, and it cost
four failed attempts to find.

Apple's `remindd` runs its own sync engine with **CRDT vector clocks per field**.
The `List` record carries a `ResolutionTokenMap`:

```json
{"map": {
  "membershipsOfRemindersInSectionsAsData":   {"counter": 1,  "modificationTime": …, "replicaID": "…"},
  "membershipsOfRemindersInSectionsChecksum": {"counter": 14, "modificationTime": …, "replicaID": "…"},
  "reminderIDsMergeableOrdering":             {"counter": 21, "modificationTime": …, "replicaID": "…"},
  "name": …, "color": …, "sortingStyle": …
}}
```

If your `counter` is below what the device holds locally, **the server accepts
the write and the device discards it**. Server-side everything looks perfect;
the reminder still shows under *Other* on the phone. Nothing warns you.

`pyicloud`'s `_generate_resolution_token_map()` hardcodes `counter: 1`, so it is
unusable here. What works:

1. **Read the existing map and only touch your own fields.** Replacing it wipes
   the vector clocks of every other field — `name`, `color`, `sortingStyle`,
   `reminderIDsMergeableOrdering`. Doing that once reduced a list from twelve
   entries to two.
2. **Set `counter` above the maximum across the whole map**, so it also wins
   when the device is ahead for that particular field.
3. **Keep `replicaID` stable.** It identifies the sync source; a fresh UUID per
   write is wrong.

Note the map's keys are camelCase and do **not** map one-to-one onto field
names: `ReminderIDs` appears as `reminderIDsMergeableOrdering`.

### The checksum, which turned out not to matter

`MembershipsOfRemindersInSectionsChecksum` is 128 hex characters (64 bytes).
It could not be reproduced. Tried against known-good Apple-written pairs:

* SHA-256 / 384 / 512, SHA3-256 / 512, BLAKE2b over the asset bytes
* the same over canonicalised JSON (sorted keys, whitespace stripped, memberships
  sorted by `memberID`), over concatenated ID pairs and triples
* the same over the field's own `fileChecksum` / `referenceChecksum` /
  `wrappingKey`
* order-independent combinations — XOR and modular sum of per-entry hashes,
  which is what a set-CRDT would plausibly use
* the checksum of the *previous* content, in case the field lags

None matched. `remi`'s notes call it "a SHA-512 checksum" without saying over
what; `remi` never computes it, because it writes to SQLite and lets Apple's
daemon produce it.

Empirically it does not matter. This project writes a SHA-512 of the new
content, and once the vector clock is right, iOS accepts the membership anyway
and re-signs the record with its own value on the next push. Setting the field
to `null` also gets accepted. Both were tested; neither was the blocker.

## Things that cannot be done from here

* **Creating, renaming or deleting lists and sections.** There are no such
  operations in the CloudKit API surface.
* **Changing a reminder's list.** `update()` does not touch the parent list, so
  a move has to be create-then-delete. Anything `create()` cannot carry —
  alarms, tags, attachments, recurrence rules, subtask links — would be lost, so
  it is better to refuse than to truncate silently.
* **URLs on reminders.** They are separate attachment records.

## Verifying a write did not break anything

Before touching section assets, dump every list's assets to a file. Afterwards,
diff the membership maps entry by entry: every `memberID` that existed before
should still exist, with the same `groupID`, except the one you changed. That
check is what turned "probably fine" into "verified".
