# Word-bank import and export

Open **Admin > Word Banks > Import**, or choose **Import Entries** on a bank.
Upload UTF-8 TXT/CSV/JSON, paste content, or enter a source URL. Map columns,
choose a new or existing active manual bank, and review before committing.
Invalid rows require explicit permission to skip. Previews are account-owned,
expire after 15 minutes, and cannot be reused after a successful import.
Source API tokens are used only for the fetch, not stored in banks or previews.

## Duplicate policies

- `preserve` (default): keep local fields and bank sense selections, add new
  definitions and memberships. Identical definition/part-of-speech pairs are
  merged, not duplicated. Metadata remains editable in the dictionary.
- `update`: update supplied display/clue/part-of-speech/properties, merge tags
  and definitions, and select the incoming sense for existing memberships.
  Fields absent from the source are not cleared; existing senses are not deleted.
- `skip`: skip words already present in the dictionary, including repeated rows.

Separate meanings are dictionary definitions, not duplicate word identities.
Each manual bank selects one sense per word. Other senses remain in the dictionary
and travel in its export. Banned and archived words/senses are never reactivated.
The summary counts processed rows, added words, merged rows, skipped/error rows,
new/duplicate definitions, and new memberships.

## Sources and limits

TXT accepts one word per line, optionally `word - definition` or tab-separated.
CSV requires a header and offers field mapping. JSON accepts an array, an
`entries`/`items`/`words` object, dictionary exports, or the portable format below.
Compatible puzzle exports extract answer entries, placements, words or rows;
non-word games are not vocabulary sources.

Supported remote sources: raw HTTP(S), GitHub file/repository/directory, GitHub
Gist, Pastebin and Puzzary bank export URLs. A Puzzary server root fetches
`/api/word-banks/export`; use `/api/word-banks/<id>/export` for one bank.
Authenticated sources require HTTPS. Normal URLs cannot contain credentials,
query strings or fragments; convert sharing URLs to raw URLs first.

One import is limited to 2 MB total UTF-8 content (including adapter responses),
5000 entries, 20 remote files/directories, 100 fields per object, 100 definitions
per word, 100 tags and 12 levels of metadata. Repositories exceeding limits fail
instead of silently truncating. Select a smaller folder/file in that case.
Words accept Latin letters/digits, spaces, apostrophes and hyphens; other scripts
are reported as invalid rather than silently damaged by dictionary normalization.

HTTP connections pin a checked resolved address, validate every redirect, do not
use proxy environment variables and do not forward tokens across origins.
Loopback, link-local, reserved, multicast and unspecified addresses are rejected.
Private-network sources require an operator-configured exact hostname/IP allowlist:
`WORD_BANK_IMPORT_ALLOWED_HOSTS=puzzary-source.example.com,puzzles.example.net`.
Only ports 80, 443 and 8790 are allowed. This deliberately does not change existing
dictionary/puzzle import services or periodic synchronization configuration.

## Portable format

Download **Export** from a bank, or GET `/api/word-banks/<id>/export`.
GET `/api/word-banks/export` returns `puzzary.word-bank.bundle.v1` with a `banks`
array of the following objects. Automatic banks export a snapshot of active matches.

```json
{
  "schema": "puzzary.word-bank.v1",
  "bank": {
    "name": "Weather",
    "description": "Weather vocabulary",
    "language": "en",
    "license": "CC0-1.0",
    "attribution": "Example creator",
    "tags": [{"name": "Weather", "tag_type": "Theme"}]
  },
  "entries": [{
    "word": "cloud",
    "display_text": "Cloud",
    "tags": [{"name": "Weather", "tag_type": "Theme"}],
    "properties": {"difficulty": "easy", "alternate_answers": [], "custom": {}},
    "definitions": [{
      "definition": "A visible mass of suspended water droplets.",
      "part_of_speech": "Noun",
      "clues": ["Look above you."],
      "tags": [],
      "properties": {"language": "en"}
    }],
    "selected_definition": 0
  }]
}
```

`selected_definition` is the zero-based imported sense used by this bank. Optional
source/license/theme/difficulty/language/alternate answers/notes and extension
fields are preserved in properties; no embedded code is executed. Import histories
record source URLs, importer and dates. Source permissions are not imported: local
`content.view`, `content.manage` and (for bearer requests) `api.use` remain authoritative.

## API workflow

POST `/api/word-banks/import/preview` with `source_text` or `source_url`, optional
`api_token`, `format`, `mapping` (destination field to source column), `bank_id`
or a new bank `name`/`description`, attribution fields, and optional puzzle `plugin`.
It returns `preview_id`, detected columns/mapping, valid entries/warnings and errors.
Nothing is saved. POST `/api/word-banks/import/commit` with that `preview_id`,
`policy` and optionally `skip_invalid: true`. Both require `content.manage`.
There is no auto-reimport; local edits are retained unless update is explicitly chosen.

## Plugin contract

Word-bank plugins retain `source_requirements=("word_bank",)` and can additionally
declare `word_bank_fields`, `word_bank_required_fields` and a callable
`word_bank_validator(portable_entry) -> list[str]`. Import review reports required
field/validator warnings for the selected plugin. These field declarations appear
in `/api/puzzle-types`; generation source entries keep their dictionary ORM
word/definition and expose merged `properties` for future plugin-specific filters.
Existing multiple-bank selection, word limits and excluded-tag filters also work
with imported memberships, with no provider-specific game changes.
