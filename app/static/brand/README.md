# Puzzary brand mark

`puzzary-mark.svg` is the editable master for the Puzzary application icon. The
single path draws a maze-like **P**; the mint circle and diamond represent the
start and finish of a puzzle.

Palette:

- Evergreen: `#0d3b36`
- Warm white: `#f7f3e8`
- Mint: `#63d5c3`

The contained evergreen field keeps the mark readable on light and dark browser
surfaces. It uses only original geometric artwork and no third-party trademarks.
The artwork is licensed with Puzzary under Apache-2.0.

Regenerate every derived web asset from the repository root:

```console
python scripts/export_brand_assets.py
```

The exporter uses the project's pinned Playwright Chromium build. Standard icons
keep the rounded field and transparent corners. The maskable icon expands the
same field to the canvas edge while preserving the mark inside the safe zone.
Increment `BRAND_ASSET_VERSION` in `scripts/export_brand_assets.py` and the
`?v=` references in `base.html` and `site.webmanifest` when the artwork changes.
