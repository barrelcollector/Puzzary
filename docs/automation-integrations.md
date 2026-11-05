# Home Assistant and print-system automation

Puzzary exposes one authenticated API for Home Assistant, print services, and other automation clients. Examples below use the reserved documentation host `https://puzzary.example.test` and placeholder identifiers. Replace them at deployment time; never commit an API token or a real private hostname.

## Authentication and permissions

Create a dedicated Puzzary account for each automation client and issue its API token from **Admin > Users > Security**. Send the token as a bearer credential:

```text
Authorization: Bearer YOUR_ACCOUNT_API_TOKEN
```

Bearer requests also require the account's `api.use` capability. Reading print manifests requires `library.view` and `puzzles.print`; triggering generation additionally requires `puzzles.generate` and the applicable create/publish permissions. Keep generation and read-only printing on separate accounts when they do not need the same authority.

## Automation workflow

Discover the generation contract before constructing a request:

```bash
curl --fail --silent \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  https://puzzary.example.test/api/puzzle-types
```

Generate a reproducible puzzle:

```bash
curl --fail --silent \
  -X POST \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"puzzle_type":"takuzu","difficulty":"easy","seed":"automation-example"}' \
  https://puzzary.example.test/api/puzzles/generate
```

The response includes the new puzzle ID, safe play and print payloads, publication state, and canonical URL. Puzzle generation never returns the solution payload.

Request one spoiler-free print manifest for selected puzzles and a published issue bundle:

```bash
curl --fail --silent \
  -X POST \
  -H "Authorization: Bearer $PUZZARY_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"puzzle_ids":["PUZZLE_ID_1","PUZZLE_ID_2"],"issue_ids":["ISSUE_ID"]}' \
  https://puzzary.example.test/api/print/manifest
```

Or resolve the latest available printable puzzle and latest published issue:

```json
{
  "latest_puzzle": true,
  "latest_issue": true
}
```

The manifest has schema `puzzary.print-manifest.v1`. Each puzzle job contains its `print_payload`, layout metadata, canonical page, browser-print URL, short-lived authenticated render URL, and API PDF URL. Each issue job contains its immutable ordered module snapshot and combined PDF URL. Duplicate selectors are collapsed while preserving requested order. Draft issues are rejected because they do not have immutable bundle snapshots.

Fetch `pdf_url` with the same bearer header. A `render_url` carries a puzzle-specific token that expires after 15 minutes and should be treated as sensitive; request a new manifest instead of storing it. Print feeding, cutter commands, queues, retries, and physical-printer status remain the print service's responsibility.

## Home Assistant example

These examples follow Home Assistant's official [RESTful Command](https://www.home-assistant.io/integrations/rest_command/) and [RESTful Sensor](https://www.home-assistant.io/integrations/sensor.rest/) configuration contracts.

Store the complete bearer value in `secrets.yaml`:

```yaml
puzzary_authorization: "Bearer YOUR_ACCOUNT_API_TOKEN"
```

Define REST commands with the public or LAN base URL appropriate for your installation:

```yaml
rest_command:
  puzzary_generate_easy_takuzu:
    url: "https://puzzary.example.test/api/puzzles/generate"
    method: POST
    headers:
      Authorization: !secret puzzary_authorization
      Content-Type: "application/json"
    payload: '{"puzzle_type":"takuzu","difficulty":"easy"}'

  puzzary_latest_print_manifest:
    url: "https://puzzary.example.test/api/print/manifest"
    method: POST
    headers:
      Authorization: !secret puzzary_authorization
      Content-Type: "application/json"
    payload: '{"latest_puzzle":true}'
```

These commands can be called from a Home Assistant script or automation. Use a `response_variable` when later actions need the returned puzzle ID, manifest, or PDF URL. Do not place bearer tokens in URLs, notification text, entity state, or logs.

For a read-only latest-puzzle sensor, use the REST integration:

```yaml
rest:
  - resource: "https://puzzary.example.test/api/puzzles/latest?printable=true"
    headers:
      Authorization: !secret puzzary_authorization
    scan_interval: 300
    sensor:
      - name: "Puzzary latest printable puzzle"
        value_template: "{{ value_json.title }}"
        json_attributes:
          - id
          - puzzle_type
          - difficulty
          - url
```

Reload or restart Home Assistant as required for the configuration section you changed, then exercise the script manually before attaching it to a schedule or physical printer.

## Existing issue and print endpoints

- `GET /api/puzzles/latest?printable=true` returns the newest accessible printable puzzle.
- `GET /api/puzzles/{id}` returns one puzzle's safe print payload.
- `GET /api/puzzles/{id}/print-preview` returns layout and print/render/PDF URLs.
- `GET /api/puzzles/{id}/print.pdf` downloads one rendered PDF.
- `GET /api/issues/latest` returns the latest published issue and ordered module payloads.
- `GET /api/issues/{id}` returns a selected issue.
- `GET /api/issues/{id}/print.pdf` downloads the combined issue PDF.
- `POST /api/issues`, `PUT /api/issues/{id}`, and `POST /api/issues/{id}/publish` create and publish bundles for authorized automation accounts.

All selections are authorized server-side. Draft, archived, scheduled, unlisted, password-protected, and sequential-series content keeps its normal Puzzary access rules.
