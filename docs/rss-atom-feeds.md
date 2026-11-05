# Private RSS and Atom feeds

Each signed-in Puzzary user can create independently revocable puzzle feeds at
**Account -> Manage puzzle feeds**. A feed is available in both RSS 2.0 and Atom
1.0, so the same selection can be used with most feed readers and automation
tools.

## Security model

The URL is a scoped credential. It contains a random secret that is shown only
when the feed is created or rotated; Puzzary stores only its SHA-256 hash. Feed
tokens are separate from account passwords, API tokens, and browser sessions.
Rotating a feed immediately invalidates its previous URLs, and revoking it
permanently disables the token. Disabling the owner or removing their library
permission also disables the feed.

Use an HTTPS public base URL before copying a feed URL to another device. The
feed response is marked private and `no-store`, and the production Puzzary
server redacts feed secrets from Uvicorn access logs. Reverse proxies and other
upstream services should also redact paths matching `/feeds/*.(rss|atom)`.

## Contents and filters

A feed may be filtered by puzzle type, theme, tag, creator, or a combination of
those fields. It can return the latest 10, 25, or 50 entries. Entries include
only:

- puzzle title and canonical link;
- puzzle type, difficulty, theme, and non-clue tags;
- creator and publication date.

Puzzle prompts, play and print payloads, answers, solutions, hints, and clue
tags are deliberately excluded. Draft, scheduled, unlisted, archived,
password-protected, and locked sequential-series puzzles are omitted. A
scheduled puzzle appears after its publication time, and an archived puzzle
disappears. Feed entry IDs are stable across token rotation and format changes.

Feeds describe the currently available puzzle catalog; they do not announce
answer updates, deadlines, or puzzle edits. Each token is rate limited per
Puzzary process. The defaults are 120 requests in five minutes and can be
changed with `PUZZARY_FEED_RATE_LIMIT` and
`PUZZARY_FEED_RATE_WINDOW_SECONDS`.

## Reader setup

Create a feed, select **Copy** beside Atom or RSS, and paste the URL into the
reader's add-subscription screen. If the URL is lost, rotate the feed and use
the newly displayed URL. Existing URLs cannot be reconstructed because the
secret is not stored.
