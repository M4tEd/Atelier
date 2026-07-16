# Collection Manager Specification

Version 2.0 — 2026-07-10

## Product

Collection Manager is a Windows-first, single-user desktop application that replaces a manually
maintained text catalog with a local SQLite library. It stores metadata and rating history only;
it never moves, renames, or modifies an artist's files. At the user's explicit request it may
read a selected local folder to calculate storage metadata.

The library contains independent Videos and Images catalogs. An artist appears at most once in
each catalog and may have different metadata, rating state, and history in the two catalogs.
Within one import, source lines such as “Early”, “Advanced”, “Tutorials”, “Free”, “Retired”, or
“Collection” that refer to the same person are duplicate candidates, not separate records.

The first release is distributed as a portable Windows folder/ZIP. At first launch the user
chooses a library directory, defaulting to Documents. The SQLite database and backups live in
that directory; AppData contains preferences and the selected directory only.

## Artist record

Each artist stores:

- Catalog membership: Videos or Images.
- Canonical name unique within that catalog, compared case-insensitively after whitespace
  normalization.
- One of six ordered tiers: Boring, Fell Off, Worth Revisiting, Unsavable Bangers, Bangers, or
  Cream of the Crop.
- Current point buffer.
- Last portfolio-update date, last evaluated date, and date added.
- Folder size, entered manually or calculated from a selected local folder, represented as a
  numeric value, unit, and exact/at-least/approximate qualifier.
- Heavy status (`yes`, `no`, or `unknown`) and compressed flag.
- Ordered tags, notes, reference URL, and optional local folder path.
- Complete point and tier histories.
- Created, updated, and soft-deleted timestamps.

Deleted artists move to Trash and can be restored. Permanent deletion is an explicit separate
operation.

## Ratings

### Vibe updates

A good update is exactly +1 and a bad update is exactly -1. The user must enter or confirm the
update date and a human-readable reason. No date or point changes happen silently.

### Rule suggestions

Recalculate detects—but does not apply—eligible adjustments:

- -1 for each full inactivity anniversary since `last_updated`.
- -1 while a heavy artist folder is uncompressed.
- +1 while at least five distinct normalized tags exist.

Suggestions are individually selectable. Accepted effects are identified by stable rule keys so
recalculation is idempotent. When an accepted condition clears, recalculation offers the inverse
adjustment; it does not reverse the point automatically.

### Tier shifts

At +3 or -3, if a legal destination exists, an ordinary point action offers Proceed, Later, and
Cancel. Proceed moves one tier and resets points to zero. Later retains the balance and exposes
the artist in Attention Needed. Cancel rolls back the triggering point action.

Batch rule changes show one summary and add legal shifts to Attention Needed. Bangers at a
negative threshold moves directly to Fell Off. Other shifts are adjacent. Cream of the Crop with
positive points and Boring with negative points have no shift prompt because no destination
exists; their balances are retained.

Manual tier override requires a reason, resets points, and logs both changes. Imported non-zero
points become a single “Legacy opening balance” event; unknown historic reasons are never
invented.

## Legacy import

Import is a previewed transaction:

1. Parse every non-header line.
2. Normalize metadata and surface ambiguity.
3. Detect exact and likely duplicate artists.
4. Resolve every duplicate group.
5. Preview final unique records.
6. Back up an existing library.
7. Commit artists and an import report atomically.

The parser accepts ISO and named-month dates, missing metadata, empty parentheses, URLs in
brackets, numeric points, sizes, notes, tags, mixed parentheses, bracketed notes, and bare names.
Values such as `5.8GB+` and `9GB+` are heavy. `4+ GB` is indeterminate and must be reviewed.
Heavy-related words in notes create warnings rather than silently modifying fields.

Duplicate detection normalizes case, punctuation, spacing, leading `The`, collapsed names, and
known variant suffixes. It only proposes groups. A user may mark a proposed group “Different
artists”. For a true group, guided resolution requires a canonical name, authoritative tier and
points, review of the newest date and other scalar values, unioned tags, and combined distinct
notes. Source variants do not become children; the import report records the resolution.

Every import explicitly targets the active Videos or Images tab. Re-import matches canonical
names case-insensitively within that catalog only. New names are added, unchanged records are
skipped, and changed records are reviewed. Point and tier differences create legacy-merge
history. Missing source names never delete database artists.

## Canonical export

Export writes the active catalog only, with tiers highest-to-lowest and names case-insensitively
within tiers. The file is UTF-8 with Windows line endings. Null fields, zero points, and empty
groups are omitted.

Canonical example:

```text
Artist Name [2026-01-15] (points: 2) (size: 5.8 GB+) (note: "text") (url: "https://example.com") [tag1, tag2]
```

Quoted values use JSON string escaping. The importer accepts both canonical and original legacy
syntax. A semantic round trip compares exportable metadata, not IDs, histories, timestamps,
folder paths, or import reports.

## Interface

The default-dark main window contains Videos and Images tabs above shared navigation for All
Artists, each tier, Attention Needed, and Trash; catalog-scoped search and filters; a sortable
compact table; and an artist detail/editor area. Import, export, add, update, recalculate,
history, open-folder, trash, and restore actions operate on the active catalog without navigating
to separate dashboards.

Choosing or explicitly recalculating a local folder measures its logical regular-file bytes on a
background worker. Descendant links are not followed. Complete scans populate exact storage
metadata; scans with unreadable or vanished entries populate a reviewed lower bound. Failed or
cancelled scans leave existing metadata unchanged, and results are not persisted until the user
saves the artist.

The default All Artists ordering follows the tier ladder from best to worst. Artists within each
tier are sorted alphabetically. Clicking another table header temporarily selects that column's
normal sort instead.

Keyboard shortcuts include Ctrl+N (add), Ctrl+F (search), Ctrl+R (recalculate), Ctrl+E (export),
and one-level Ctrl+Z for the latest reversible action in the session.

## Storage and audit guarantees

SQLite foreign keys and WAL mode are enabled. All writes are transactional. Imports and schema
migrations create timestamped backups. Every point mutation, reset, merge adjustment, reversal,
and tier change is represented in its corresponding history.

## Acceptance

- All 208 supplied source lines parse without an unparseable result.
- Each line becomes a unique candidate or a member of a user-resolved duplicate group.
- No duplicate group is merged automatically or committed unresolved.
- The same canonical name can exist independently in Videos and Images but not twice in either.
- Canonical export re-imports into its selected catalog without changing exportable fields.
- Recalculation never duplicates an applied effect and never changes points without approval.
- All legal tier paths, Bangers-to-Fell-Off, boundary suppression, deferral, reset, and undo work.
- Trash restore, import rollback, database backup, unavailable-library handling, and Windows
  portable startup are verified.
- Local-folder measurement remains responsive, ignores stale results after changing artist or
  path, and persists only after Save changes.

## Deferred

Arbitrary user-defined catalogs, artist/collection hierarchies, moving records between catalogs,
folder watching, allocated-on-disk accounting, dashboards, thumbnails, cloud sync, accounts,
automatic text synchronization, and management of binary collection files are outside the MVP.
