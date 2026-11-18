# Home Assistant integration

The Home Assistant integration is released independently at
https://github.com/barrelcollector/puzzary-home-assistant. Use that repository's
README for current HACS and manual installation instructions. This page records
the Puzzary-side behavior and API expectations.

The `puzzary-home-assistant` custom integration connects one Home Assistant
configuration entry to one Puzzary account. It uses Puzzary's authenticated API;
the API token is stored in Home Assistant's config entry and is never placed in
entity attributes.

## Install

1. Add `https://github.com/barrelcollector/puzzary-home-assistant` as a HACS
   custom Integration repository and install Puzzary, or copy its
   `custom_components/puzzary` folder manually.
2. Restart Home Assistant after installation or upgrade.
3. Open **Settings -> Devices & services -> Add integration** and select
   **Puzzary**.
4. Enter a complete Puzzary base URL and an API token issued from Puzzary's user
   administration page.

Use a URL reachable from the Home Assistant host or container. `localhost`
refers to Home Assistant itself, not the Puzzary server, when the applications
run in separate containers.

## Entities

The integration polls once per minute and shares the result between all
entities. It creates sensors for:

- puzzles played
- puzzles completed
- puzzles in progress
- attempts
- correct attempts
- the latest printable puzzle

It also creates **Latest printable puzzle**, an image entity. Puzzary renders
the same spoiler-free print sheet used by its print preview as a PNG. Home
Assistant fetches that image only when the entity is viewed or an image action
requests it; the image is cached until the latest puzzle changes.

## Media folders

Puzzary also appears in Home Assistant's Media browser. Printable puzzles are
organized into **Active puzzles**, **In-progress puzzles**, **Completed
puzzles**, and **Archived puzzles**, followed by a folder for each puzzle type.
Non-printable digital games, locked password-protected puzzles, and currently
locked sequential-series stages are not offered as printable media.

The integration proxies a selected PNG through Home Assistant and gives media
players a short-lived signed URL. The Puzzary API token is never placed in the
media item or its URL. Puzzle images are rendered only when selected rather
than eagerly rendering every puzzle while a folder is opened.

## Actions

Each action requires the Puzzary configuration entry so automations remain
unambiguous when more than one server or account is configured.

- `puzzary.refresh`
- `puzzary.generate_puzzle`
- `puzzary.publish_puzzle`
- `puzzary.archive_puzzle`
- `puzzary.unarchive_puzzle`

Generation accepts the same type, difficulty, theme, word-bank slugs, seed,
visibility, completion message, and type-specific `generation_options` as the
Puzzary generation API. Actions support response data, so scripts can capture
the generated or updated puzzle ID and URL.

Example action data:

```yaml
action: puzzary.generate_puzzle
data:
  config_entry_id: 01EXAMPLEENTRYID
  puzzle_type: word_search
  difficulty: easy
  word_bank_slugs:
    - weather
  seed: morning-puzzle
response_variable: generated
```

`generate_puzzle`, `publish_puzzle`, `archive_puzzle`, and `unarchive_puzzle`
require the corresponding generation, publish, or archive permission. An
administrator token has all of these. A normal API-enabled account can use the
statistics, latest-puzzle sensor, image entity, and refresh action.

## Upgrade or remove

To upgrade, use HACS or replace the installed `config/custom_components/puzzary`
folder from the integration repository and restart Home Assistant. To remove it, delete
the Puzzary entry from **Settings -> Devices & services**, remove the folder,
and restart Home Assistant.
