# Offline puzzle play

Puzzary supports opt-in, per-browser downloads for eligible solo puzzles. A
signed-in player chooses **Available offline** on a puzzle, then manages saved
puzzles, the local item limit, synchronization, removal, and conflicts from
**Offline** in the main navigation.

A common private-hosting workflow is to download puzzles while connected to a
home LAN, play while away, and synchronize after returning. The server does not
need public internet exposure, but PWA and service-worker features on another
device still require a trusted HTTPS origin. See the
[LAN-only offline travel guide](lan-only-offline.md).

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

Players can check and solve every downloaded puzzle without a connection.
Modules either validate from public puzzle rules (`local_rules`) or download a
puzzle-specific salted SHA-256 commitment (`solution_hash`) that lets the
browser compare the completed progress without receiving the answer key.
Completion receives authoritative server confirmation and is recorded on the
account after synchronization. Hints, reveals, uploads, and Group Solve remain
online-only.

A solution commitment is intentionally less secret than keeping all checking
data on the server. It does not reveal the answer directly, but a person with
the downloaded file can test guesses, and small answer spaces may be practical
to brute-force. Puzzle owners can turn off **Offline play** for an individual
puzzle in the web UI. A disabled puzzle cannot be downloaded and its solution
commitment is never sent to the browser. Modules that cannot run from public
clues alone may instead provide module-specific `offline_game_data`.
Treat that data as browser-visible and disable offline play for any puzzle whose
game state must remain server-only.

## Plugin contract

`PuzzleType` defaults to no offline support. An eligible module declares:

- `supports_offline=True`;
- `offline_progress_fields`, limited to serializable `answers` and/or
  `found_words` state accepted by its normal progress normalizer;
- `offline_validation="local_rules"` when the client can validate from public
  constraints, or `offline_validation="solution_hash"` together with an
  `offline_solution_state` callback that returns the canonical progress state.
  Sequential modules may also provide `offline_solution_parts` for stage-level
  commitments, and modules may provide `offline_game_data` for browser-side rules;
- `offline_hash_mode="exact"` for exact answer maps, or `"filled_cells"` when
  only filled cells are significant;
- `offline_notes` explaining which actions still require a connection; and
- every required script in `play_script`, so the download can pre-cache it.

The legacy `server` validation value remains loadable for compatibility but is
not eligible for download, because it cannot provide a complete offline game.
An offline-capable module must still treat the server normalizer, counter, and
access checks as authoritative after synchronization. It must not expose raw
solution data or claim that realtime/collaborative actions work offline.
