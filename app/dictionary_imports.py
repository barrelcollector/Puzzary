from __future__ import annotations

import csv
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from io import StringIO
from typing import Any


MAX_FETCH_BYTES = 2_000_000
IMPORTABLE_EXTENSIONS = (".json", ".csv", ".txt", ".md")


@dataclass
class ImportedDefinition:
    definition: str = ""
    part_of_speech: str = ""
    example: str = ""
    clues: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportedWord:
    word: str
    display_text: str = ""
    clue: str = ""
    part_of_speech: str = ""
    tags: list[str] = field(default_factory=list)
    definitions: list[ImportedDefinition] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)


def fetch_dictionary_source(source_url: str, source_type: str = "auto", source_api_token: str = "") -> list[dict[str, str]]:
    url = normalize_source_url(source_url)
    detected_type = (source_type or "auto").strip().lower()
    if detected_type == "auto":
        detected_type = detect_source_type(url)
    if detected_type == "puzzary":
        return fetch_puzzary_dictionary(url, source_api_token)
    if detected_type == "github":
        return fetch_github_source(url)
    return [fetch_url(url, source_api_token)]


def normalize_source_url(source_url: str) -> str:
    url = source_url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Import source must be an http or https URL")
    if parsed.netloc.lower() == "pastebin.com" and not parsed.path.startswith("/raw/"):
        paste_id = parsed.path.strip("/").split("/")[0]
        if paste_id:
            return f"https://pastebin.com/raw/{paste_id}"
    if parsed.netloc.lower() == "github.com" and "/blob/" in parsed.path:
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 5:
            owner, repo, _blob, branch = parts[:4]
            file_path = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
    return url


def detect_source_type(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if "github.com" in host or "raw.githubusercontent.com" in host:
        return "github"
    if parsed.path.rstrip("/").endswith("/api/dictionary") or parsed.path.rstrip("/") == "":
        return "puzzary"
    return "generic"


def fetch_url(url: str, bearer_token: str = "") -> dict[str, str]:
    headers = {"User-Agent": "Puzzary-Dictionary-Importer/1.0"}
    if bearer_token.strip():
        headers["Authorization"] = f"Bearer {bearer_token.strip()}"
        headers["x-puzzary-api-token"] = bearer_token.strip()
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "")
            content = response.read(MAX_FETCH_BYTES + 1)
    except urllib.error.URLError as error:
        raise ValueError(f"Could not fetch import source: {error}") from error
    if len(content) > MAX_FETCH_BYTES:
        raise ValueError("Import source is too large")
    return {
        "url": url,
        "content_type": content_type,
        "text": content.decode("utf-8", errors="replace"),
    }


def fetch_puzzary_dictionary(base_url: str, api_token: str = "") -> list[dict[str, str]]:
    parsed = urllib.parse.urlparse(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/dictionary/export"):
        return [fetch_url(base_url, api_token)]
    if path.endswith("/api/dictionary"):
        endpoint = base_url
    else:
        export_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/api/dictionary/export", "", "", ""))
        try:
            return [fetch_url(export_url, api_token)]
        except ValueError:
            endpoint = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/api/dictionary", "", "", ""))
    fetched: list[dict[str, str]] = []
    offset = 0
    while offset < 5000:
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}include_archived=false&include_banned=false&limit=500&offset={offset}"
        item = fetch_url(url, api_token)
        fetched.append(item)
        try:
            payload = json.loads(item["text"])
        except json.JSONDecodeError:
            break
        count = len(payload.get("items", [])) if isinstance(payload, dict) else 0
        total = int(payload.get("total", count)) if isinstance(payload, dict) else count
        offset += count
        if count == 0 or offset >= total:
            break
    return fetched


def fetch_github_source(url: str) -> list[dict[str, str]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() == "raw.githubusercontent.com":
        return [fetch_url(url)]
    if parsed.netloc.lower() != "github.com":
        return [fetch_url(url)]

    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError("GitHub import URL must include an owner and repository")
    owner, repo = parts[:2]
    if len(parts) >= 5 and parts[2] == "blob":
        normalized = normalize_source_url(url)
        return [fetch_url(normalized)]

    branch = "main"
    subpath = ""
    if len(parts) >= 4 and parts[2] == "tree":
        branch = parts[3]
        subpath = "/".join(parts[4:])

    tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    try:
        tree = json.loads(fetch_url(tree_url)["text"])
    except ValueError:
        if branch == "main":
            tree_url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/master?recursive=1"
            tree = json.loads(fetch_url(tree_url)["text"])
            branch = "master"
        else:
            raise

    fetched: list[dict[str, str]] = []
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if item.get("type") != "blob":
            continue
        if subpath and not path.startswith(f"{subpath.rstrip('/')}/") and path != subpath.rstrip("/"):
            continue
        if not path.lower().endswith(IMPORTABLE_EXTENSIONS):
            continue
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        fetched.append(fetch_url(raw_url))
        if len(fetched) >= 20:
            break
    if not fetched:
        raise ValueError("No importable .json, .csv, .txt, or .md files were found in that GitHub source")
    return fetched


def parse_imported_words(sources: list[dict[str, str]]) -> list[ImportedWord]:
    words: list[ImportedWord] = []
    for source in sources:
        text = source["text"]
        content_type = source.get("content_type", "")
        source_url = source.get("url", "")
        parsed_items = parse_source_text(text, content_type, source_url)
        words.extend(parsed_items)
    return dedupe_imported_words(words)


def parse_source_text(text: str, content_type: str, source_url: str) -> list[ImportedWord]:
    stripped = text.lstrip("\ufeff\r\n\t ")
    if "json" in content_type.lower() or stripped.startswith("{") or stripped.startswith("["):
        try:
            return parse_json_payload(json.loads(stripped), source_url)
        except json.JSONDecodeError as error:
            raise ValueError(f"Import source is not valid JSON: {error}") from error
    if "csv" in content_type.lower() or source_url.lower().endswith(".csv"):
        return parse_csv_payload(text, source_url)
    return parse_text_payload(text, source_url)


def parse_json_payload(payload: Any, source_url: str) -> list[ImportedWord]:
    if isinstance(payload, dict):
        if "items" in payload:
            return parse_json_payload(payload["items"], source_url)
        if "words" in payload and isinstance(payload["words"], list):
            return parse_json_payload(payload["words"], source_url)
        item = imported_word_from_mapping(payload, source_url)
        return [item] if item is not None else []
    if isinstance(payload, list):
        words: list[ImportedWord] = []
        for item in payload:
            if isinstance(item, str):
                words.append(ImportedWord(word=item, properties={"source_url": source_url}))
            elif isinstance(item, dict):
                parsed = imported_word_from_mapping(item, source_url)
                if parsed is not None:
                    words.append(parsed)
        return words
    return []


def imported_word_from_mapping(item: dict[str, Any], source_url: str) -> ImportedWord | None:
    raw_word = first_string(item, "word", "display_text", "term", "answer", "text", "name")
    if not raw_word:
        return None
    definitions = definitions_from_mapping(item, source_url)
    return ImportedWord(
        word=raw_word,
        display_text=first_string(item, "display_text", "label", "word") or raw_word,
        clue=first_string(item, "clue"),
        part_of_speech=first_string(item, "part_of_speech", "pos"),
        tags=string_list(item.get("tags")),
        definitions=definitions,
        properties={"source_url": source_url, **dict_value(item.get("properties"))},
    )


def definitions_from_mapping(item: dict[str, Any], source_url: str) -> list[ImportedDefinition]:
    candidates = item.get("definitions")
    if candidates is None:
        candidates = item.get("meanings") or item.get("senses")
    if candidates is None and item.get("definition"):
        candidates = [item]
    definitions: list[ImportedDefinition] = []
    if isinstance(candidates, list):
        for definition in candidates:
            if isinstance(definition, str):
                definitions.append(ImportedDefinition(definition=definition, properties={"source_url": source_url}))
            elif isinstance(definition, dict):
                definitions.append(
                    ImportedDefinition(
                        definition=first_string(definition, "definition", "meaning", "text", "gloss"),
                        part_of_speech=first_string(definition, "part_of_speech", "pos"),
                        example=first_string(definition, "example", "sentence"),
                        clues=string_list(definition.get("clues")) or string_list(definition.get("hints")),
                        tags=string_list(definition.get("tags")),
                        properties={"source_url": source_url, **dict_value(definition.get("properties"))},
                    )
                )
    return [definition for definition in definitions if definition.definition.strip()]


def parse_csv_payload(text: str, source_url: str) -> list[ImportedWord]:
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample) if sample.strip() else csv.excel
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(StringIO(text), dialect=dialect)
    words: list[ImportedWord] = []
    for row in reader:
        normalized = {str(key or "").strip().lower().replace(" ", "_"): value for key, value in row.items()}
        parsed = imported_word_from_mapping(normalized, source_url)
        if parsed is not None:
            words.append(parsed)
    return words


def parse_text_payload(text: str, source_url: str) -> list[ImportedWord]:
    words: list[ImportedWord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pieces = [piece.strip() for piece in re.split(r"\t|\s+-\s+|\s+:\s+", stripped, maxsplit=1)]
        if len(pieces) == 2:
            words.append(
                ImportedWord(
                    word=pieces[0],
                    definitions=[ImportedDefinition(definition=pieces[1], properties={"source_url": source_url})],
                    properties={"source_url": source_url},
                )
            )
        else:
            words.append(ImportedWord(word=stripped, properties={"source_url": source_url}))
    return words


def dedupe_imported_words(words: list[ImportedWord]) -> list[ImportedWord]:
    merged: dict[str, ImportedWord] = {}
    for word in words:
        key = re.sub(r"[^A-Za-z0-9]+", "", word.word).lower()
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = word
            continue
        existing.tags = sorted({*existing.tags, *word.tags})
        existing.definitions.extend(word.definitions)
        existing.properties = {**existing.properties, **word.properties}
    return list(merged.values())


def first_string(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;\n]+", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
