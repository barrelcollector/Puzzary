# Group Solve

Group Solve lets several people work on the same supported puzzle. A puzzle
module must explicitly provide collaboration behavior; modules without it stay
available for solo play only.

## Host a session

From a supported puzzle, expand **Group Solve** and create a session. Depending
on the host's permissions, a session can use invite-only access, approval,
participant limits, optional guests, a guest password, or listing in the public
lobby. A public listing exposes only the information needed to choose a session,
not its code, password, hidden puzzle data, or shared answers.

Hosts can approve or remove participants, change session settings, transfer
host control to an eligible account, or close the session. Guests are scoped to
their one session and never receive account, library, or administration access.

## Join and collaborate

Join from **Group Solve**, an invite link, or a public lobby card. The shared
state is authoritative on the server. Participant focus, pointers, and recent
contributions may be shown when the module supports them, but presence data is
kept separate from saved answers.

Conflicting edits to the same part of a puzzle are rejected instead of silently
overwriting newer work. Reconnecting restores the current shared state. Group
progress is separate from each participant's personal puzzle progress,
attempts, and uploaded work.

## Privacy and availability

Draft, archived, hidden, and otherwise private puzzles are never made public by
the lobby. Password and permission checks still apply when a session is joined
through a direct link. Group Solve requires a connection and is not included in
offline puzzle downloads.

For module developers, the collaboration hooks and validation requirements are
documented in the [puzzle module contract](puzzle-modules.md). API clients should
use the live OpenAPI reference at `/docs` so their requests match the deployed
core version and installed modules.
