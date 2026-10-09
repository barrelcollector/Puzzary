# Puzzary appearance themes

Puzzary includes Light, Dark, and Follow System appearances. Signed-in users can choose an appearance on the Account page. Administrators with `themes.manage` (or the legacy `settings.manage`) can manage the server default and custom themes at `/admin/themes`.

## Portable package format

A theme package is a UTF-8 JSON file, conventionally named `<slug>.puzzary-theme.json`. Version 1 uses this shape:

```json
{
  "schema": "puzzary-theme/v1",
  "slug": "midnight-garden",
  "name": "Midnight Garden",
  "version": "1.0.0",
  "author": "Example author",
  "description": "A dark green theme.",
  "tokens": {
    "bg": "#101512",
    "panel": "#1b221e",
    "ink": "#f3f7f4"
  }
}
```

Missing tokens inherit the built-in Light values and are included when the package is exported. Supported color tokens are `bg`, `panel`, `ink`, `muted`, `line`, `accent`, `accent_text`, `focus`, `success`, `success_bg`, `danger`, `danger_bg`, `surface_muted`, `puzzle_cell`, `puzzle_grid`, and `player_1` through `player_6`. Colors must be six-digit hex values. `font` is one of `system`, `serif`, or `mono`; `radius` is 0–24 pixels; and `density` is 0.8–1.25.

Packages cannot include CSS, JavaScript, HTML, URLs, fonts, or external assets. Puzzary rejects unknown fields and tokens, packages over 64 KB, unsafe values, inadequate text contrast, duplicate player colors, and player colors without enough contrast against the page background.

## Creating and sharing

Use the live editor at `/admin/themes` to create or update a theme. Export produces the complete validated manifest, which another Puzzary administrator can install from the same page. Disabling or removing a custom theme causes affected users to fall back to the active server default; if the removed theme was the default, Puzzary falls back to Follow System.

Game plugins should style surfaces with the semantic CSS variables above. Puzzle cells use `--puzzle-cell` and `--puzzle-grid`. The first six theme `--player-*` values remain fallback colors; Group Solve normally supplies each member's stable, session-unique `color`. Participant names and numbered markers ensure attribution never relies on color alone.
