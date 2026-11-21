# Offline puzzle play

Puzzary supports opt-in, per-browser downloads for eligible solo puzzles. A
signed-in player chooses **Available offline** on a puzzle, then manages saved
puzzles, the local item limit, synchronization, removal, and conflicts from
**Offline** in the main navigation.

## Security and access

Offline HTML is rendered separately from the normal puzzle page. It contains
only the public play payload, the player's current progress, and required
static assets. It never includes solution or administrator views. Guest links,
Group Solve, password-protected puzzles, hidden or unlisted puzzles, drafts,
scheduled puzzles, and archived puzzles are not eligible. Account changes and
logout clear user-specific caches. Access and publication state are checked
again whenever queued progress synchronizes; a withdrawn puzzle is removed.

Browser storage is not durable backup. The browser may evict downloaded data,
especially under storage pressure, and Puzzary reports the browser's current
storage estimate where available.

## Progress and conflicts

Only fields declared by the puzzle module are queued. A service worker stores
the latest permitted state across reloads and submits it when connectivity
returns. The server compares both the puzzle content version and the progress
revision recorded when the download was made. If the puzzle changed or another
device saved progress, automatic synchronization stops and the player chooses
the server progress or explicitly keeps the device progress. Inaccessible,
unpublished, archived, and removed puzzles fail closed.

Completion always receives authoritative server confirmation after sync. A
module may additionally declare `local_rules` validation when correctness can
be derived from public puzzle rules without shipping an answer key. Other
modules can save state offline but checks, hints, and reveals remain online.

## Plugin contract

`PuzzleType` defaults to no offline support. An eligible module declares:

- `supports_offline=True`;
- `offline_progress_fields`, limited to serializable `answers` and/or
  `found_words` state accepted by its normal progress normalizer;
- `offline_validation="local_rules"` only when the client can validate using
  public constraints, otherwise the default `server`;
- `offline_notes` explaining which actions still require a connection; and
- every required script in `play_script`, so the download can pre-cache it.

An offline-capable module must still treat the server normalizer, counter, and
access checks as authoritative. It must not expose solution data or claim that
realtime/collaborative actions work offline.
