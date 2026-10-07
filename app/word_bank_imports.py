"""Portable vocabulary parsing, preview and transactional dictionary-backed import."""
import csv
import json
import math
import re
from io import StringIO
from sqlalchemy import select
from app.models import DictionaryDefinition, DictionaryWord, PuzzleTag, WordBank, WordBankWord
from app.puzzles import get_puzzle_type
from app.word_bank_sources import MAX_BYTES, MAX_ENTRIES

FIELDS = ("word", "display_text", "definition", "clue", "part_of_speech", "tags", "theme", "difficulty", "language", "alternate_answers", "source", "license", "notes")
ALIASES = {"word": ("word", "answer", "term", "text", "name"), "definition": ("definition", "meaning", "gloss"), "part_of_speech": ("part_of_speech", "pos"), "clue": ("clue", "hint"), "language": ("language", "locale")}
POLICIES = {"skip", "update", "preserve"}


def core():
    from app import main
    return main


def text(value):
    return value.strip() if isinstance(value, str) else ""


def validate_structure(value, depth=0):
    if depth > 12:
        raise ValueError("Import metadata is nested too deeply")
    if isinstance(value, dict):
        if len(value) > 100:
            raise ValueError("An object can have at most 100 fields")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 120:
                raise ValueError("Metadata field names must be short strings")
            validate_structure(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_ENTRIES:
            raise ValueError("Import is limited to 5000 entries")
        for item in value:
            validate_structure(item, depth + 1)
    elif isinstance(value, str) and (len(value) > 10000 or any(ord(c) < 32 and c not in "\n\r\t" for c in value)):
        raise ValueError("Text is too long or contains control characters")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Metadata cannot contain non-finite numbers")


def tags(value):
    if isinstance(value, str):
        return [{"name": part.strip(), "tag_type": "Imported"} for part in re.split(r"[,;\n]", value) if part.strip()]
    result = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            result.append({"name": item, "tag_type": "Imported"})
        elif isinstance(item, dict) and text(item.get("name")):
            result.append({"name": item["name"], "tag_type": text(item.get("tag_type")) or "Imported"})
    if len(result) > 100 or any(len(t["name"]) > 120 or len(t["tag_type"]) > 40 for t in result):
        raise ValueError("An entry can have at most 100 tags, with names up to 120 characters")
    return result


def rows_from_json(payload):
    metadata = {}
    if isinstance(payload, dict):
        metadata = payload.get("bank") if isinstance(payload.get("bank"), dict) else {}
        schema = str(payload.get("schema", ""))
        if schema == "puzzary.word-bank.bundle.v1":
            banks = payload.get("banks", [])
            if not isinstance(banks, list) or any(not isinstance(bank, dict) for bank in banks):
                raise ValueError("Bank bundle must contain an array of bank objects")
            if len(banks) > 50:
                raise ValueError("Choose a single bank from sources with more than 50 banks")
            rows = []
            for bank in banks:
                children, child_metadata = rows_from_json(bank)
                for row in children:
                    properties = row.get("properties", {}) if isinstance(row.get("properties"), dict) else {}
                    row["properties"] = {**{key: child_metadata[key] for key in ("license", "language", "attribution", "source") if child_metadata.get(key)}, **properties, "source_bank": child_metadata.get("name", "")}
                rows.extend(children)
            return rows, metadata
        if schema in {"puzzary.puzzle.item.v1", "puzzary.puzzle.bundle.v1"}:
            rows = []
            puzzles = payload.get("items", [payload])
            if not isinstance(puzzles, list) or any(not isinstance(puzzle, dict) for puzzle in puzzles):
                raise ValueError("Puzzle bundle must contain an array of puzzle objects")
            for puzzle in puzzles:
                solution = puzzle.get("solution_payload", {})
                if not isinstance(solution, dict):
                    raise ValueError("Puzzle solution must be an object")
                entries = solution.get("entries") or solution.get("placements") or solution.get("rows") or solution.get("words") or []
                if isinstance(entries, dict):
                    entries = list(entries.values())
                if not isinstance(entries, list):
                    raise ValueError("Puzzle answers must be an array")
                rows.extend({"word": item} if isinstance(item, str) else item for item in entries if isinstance(item, (str, dict)))
            return rows, metadata
        candidates = payload.get("entries", payload.get("items", payload.get("words")))
        if candidates is None:
            candidates = [payload]
    elif isinstance(payload, list):
        candidates = payload
    else:
        raise ValueError("JSON must contain an array of words or entries")
    if not isinstance(candidates, list):
        raise ValueError("Entries must be an array")
    rows = []
    for item in candidates:
        if isinstance(item, str):
            rows.append({"word": item})
        elif isinstance(item, dict):
            # Accept both the portable format and existing API bank-link exports.
            if isinstance(item.get("word"), dict):
                item = {**item["word"], "definitions": [item["definition"]] if isinstance(item.get("definition"), dict) else item["word"].get("definitions", [])}
            rows.append(item)
        else:
            rows.append({"_error": "Entry must be a word or an object"})
    return rows, metadata


def parse_sources(sources, format_name="auto"):
    if format_name not in {"auto", "txt", "csv", "json"}:
        raise ValueError("Choose TXT, CSV, JSON or automatic detection")
    if sum(len(source["text"].encode("utf-8")) for source in sources) > MAX_BYTES:
        raise ValueError("Import content exceeds the 2 MB limit")
    rows, metadata = [], {}
    formats = set()
    for source in sources:
        content = source["text"].lstrip("\ufeff").strip()
        kind = format_name
        if kind == "auto":
            kind = "json" if content.startswith(("{", "[")) else "csv" if "csv" in source.get("content_type", "") or source.get("url", "").lower().endswith(".csv") or "," in content.split("\n", 1)[0] else "txt"
        formats.add(kind)
        if kind == "json":
            try:
                payload = json.loads(content)
            except RecursionError as error:
                raise ValueError("Import metadata is nested too deeply") from error
            validate_structure(payload)
            parsed, bank_metadata = rows_from_json(payload)
            metadata.update(bank_metadata)
        elif kind == "csv":
            try:
                dialect = csv.Sniffer().sniff(content[:4096])
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(StringIO(content), dialect=dialect)
            if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ValueError("CSV needs unique column headings")
            parsed = []
            for row in reader:
                if None in row:
                    parsed.append({"_error": "CSV row contains extra columns"})
                else:
                    parsed.append({str(key).strip(): value for key, value in row.items()})
                if len(parsed) > MAX_ENTRIES:
                    raise ValueError("Import is limited to 5000 entries")
        else:
            parsed = []
            for line in content.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = re.split(r"\t|\s+-\s+|\s+:\s+", line.strip(), maxsplit=1)
                parsed.append({"word": parts[0], **({"definition": parts[1]} if len(parts) == 2 else {})})
        for row in parsed:
            row = {**row, "_source_url": source.get("url", "pasted-content")}
            rows.append(row)
            if len(rows) > MAX_ENTRIES:
                raise ValueError("Import is limited to 5000 entries")
    if not rows:
        raise ValueError("The source contains no entries")
    columns = sorted({key for row in rows for key in row if not key.startswith("_")})
    if len(columns) > 100:
        raise ValueError("Imports can have at most 100 columns")
    mapping = {field: next((column for alias in ALIASES.get(field, (field,)) for column in columns if column.lower().replace(" ", "_") == alias), "") for field in FIELDS}
    return {"rows": rows, "metadata": metadata, "columns": columns, "mapping": mapping, "formats": sorted(formats)}


def mapped_entry(row, mapping):
    validate_structure(row)
    values = {field: row.get(mapping.get(field, ""), "") for field in FIELDS}
    raw_word = text(values["word"])
    if row.get("_error"):
        raise ValueError(row["_error"])
    if not raw_word or len(raw_word) > 80:
        raise ValueError("Word must be 1 to 80 characters")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 '\-]*", raw_word):
        raise ValueError("Word contains unsupported characters (use Latin letters, digits, spaces, apostrophes or hyphens)")
    word_key = core().normalize_dictionary_word(raw_word)
    if not word_key:
        raise ValueError("Word has no playable letters")
    display = text(values["display_text"]) or raw_word
    if len(display) > 80 or len(text(values["part_of_speech"])) > 40:
        raise ValueError("Display text or part of speech is too long")
    properties = dict(row.get("properties", {})) if isinstance(row.get("properties"), dict) else {}
    for key in ("theme", "difficulty", "language", "alternate_answers", "source", "license", "notes"):
        if values.get(key):
            properties[key] = values[key]
    # Keep unmapped extension fields for plugins without interpreting or executing them.
    custom = {key: value for key, value in row.items() if not key.startswith("_") and key not in set(mapping.values()) | {"definitions", "properties", "selected_definition", "clues", "example", "archived", "banned", "id", "position", "created_at"}}
    if custom:
        properties["custom"] = {**properties.get("custom", {}), **custom} if isinstance(properties.get("custom", {}), dict) else custom
    candidates = row.get("definitions", [])
    if values.get("definition"):
        candidates = [{"definition": values["definition"], "part_of_speech": values["part_of_speech"], "clues": [values["clue"]] if values["clue"] else []}, *(candidates if isinstance(candidates, list) else [])]
    if not isinstance(candidates, list) or len(candidates) > 100:
        raise ValueError("Definitions must be an array of at most 100 senses")
    definitions, signatures = [], set()
    for candidate in candidates:
        definition = {"definition": candidate} if isinstance(candidate, str) else candidate
        if not isinstance(definition, dict) or not text(definition.get("definition")):
            raise ValueError("Definition text is missing")
        definition = {**definition, "definition": text(definition["definition"]), "part_of_speech": text(definition.get("part_of_speech")) or text(values["part_of_speech"]), "tags": tags(definition.get("tags"))}
        if len(definition["definition"]) > 10000 or len(definition["part_of_speech"]) > 40:
            raise ValueError("Definition or part of speech is too long")
        if not isinstance(definition.get("properties", {}), dict):
            raise ValueError("Definition properties must be an object")
        clues = definition.get("clues", definition.get("properties", {}).get("clues", []))
        if isinstance(clues, str):
            clues = clues.splitlines()
        if not isinstance(clues, list) or len(clues) > 30 or any(not isinstance(clue, str) or len(clue) > 10000 for clue in clues):
            raise ValueError("Clues must be an array of at most 30 short strings")
        definition["clues"] = [clue.strip() for clue in clues if clue.strip()]
        signature = core().normalize_definition_signature(definition["definition"], definition["part_of_speech"])
        if signature not in signatures:
            definitions.append(definition)
            signatures.add(signature)
    selected = row.get("selected_definition", 0)
    if not isinstance(selected, int) or selected < 0 or (definitions and selected >= len(definitions)):
        raise ValueError("Selected definition index is invalid")
    return {"word": word_key, "display_text": display, "clue": text(values["clue"]), "part_of_speech": text(values["part_of_speech"]), "tags": tags(values["tags"]), "definitions": definitions, "selected_definition": selected, "properties": properties, "source_url": row.get("_source_url", ""), "supplied_fields": [field for field in ("display_text", "clue", "part_of_speech") if text(values[field])]}


def preview(db, parsed, mapping, bank_id=None, plugin_slug=""):
    if set(mapping) - set(FIELDS) or any(not isinstance(value, str) or value and value not in parsed["columns"] for value in mapping.values()):
        raise ValueError("Field mapping contains an unknown column")
    bank = db.get(WordBank, bank_id) if bank_id else None
    if bank_id and (not bank or core().word_bank_mode(bank) != "manual" or bank.archived_at):
        raise ValueError("Select an active manual word bank")
    plugin = get_puzzle_type(plugin_slug) if plugin_slug else None
    if plugin_slug and (not plugin or "word_bank" not in plugin.source_requirements):
        raise ValueError("Choose a compatible word-bank puzzle type")
    existing = {word.word.lower(): word for word in db.scalars(select(DictionaryWord))}
    seen, entries, errors = set(), [], []
    for number, row in enumerate(parsed["rows"], 1):
        try:
            entry = mapped_entry(row, mapping)
            warnings = []
            if not entry["definitions"]:
                warnings.append("No definition supplied")
            if entry["word"] in seen:
                warnings.append("Repeated word in this source; definitions will be combined")
            word = existing.get(entry["word"])
            if word:
                warnings.append("Existing dictionary word")
                if word.banned_at or word.archived_at:
                    warnings.append("Banned or archived word remains excluded from generation")
                signatures = {core().normalize_definition_signature(d.definition, d.part_of_speech) for d in word.definitions}
                if any(core().normalize_definition_signature(d["definition"], d["part_of_speech"]) in signatures for d in entry["definitions"]):
                    warnings.append("Duplicate definitions will not be added again")
            if bank and any(link.word.word.lower() == entry["word"] for link in bank.word_links):
                warnings.append("Already in destination bank")
            if plugin:
                for field in plugin.word_bank_required_fields:
                    value = entry["definitions"] if field == "definition" else entry.get(field, entry["properties"].get(field))
                    if not value:
                        warnings.append(f"{plugin.label} requires {field}")
                if plugin.word_bank_validator:
                    warnings.extend(plugin.word_bank_validator(entry))
            seen.add(entry["word"])
            entries.append({**entry, "row": number, "warnings": warnings})
        except (ValueError, TypeError) as error:
            errors.append({"row": number, "error": str(error)})
    return {"entries": entries, "errors": errors, "total": len(parsed["rows"]), "valid": len(entries), "bank_id": bank_id, "plugin": plugin_slug}


def merge_tags(db, target, values):
    for value in values:
        name, kind = value["name"].strip(), core().normalize_tag_type(value["tag_type"])
        tag = db.scalar(select(PuzzleTag).where(PuzzleTag.name == name, PuzzleTag.tag_type == kind))
        if not tag and name:
            tag = PuzzleTag(name=name, tag_type=kind, slug=core().unique_slug(db, PuzzleTag, core().typed_tag_slug(kind, name)[:65]))
            db.add(tag)
            db.flush()
        if tag and all(existing.id != tag.id for existing in target.tags):
            target.tags.append(tag)


def commit_import(db, actor, reviewed, name, description, metadata, policy):
    if policy not in POLICIES:
        raise ValueError("Choose a valid duplicate policy")
    if not reviewed["entries"]:
        raise ValueError("There are no valid entries to import")
    bank_id = reviewed.get("bank_id")
    # Lock the destination and existing dictionary identities for concurrent imports.
    bank = db.scalar(select(WordBank).where(WordBank.id == bank_id).with_for_update()) if bank_id else None
    if bank_id and (not bank or bank.archived_at or core().word_bank_mode(bank) != "manual"):
        raise ValueError("Destination bank is no longer available")
    if not bank:
        if not text(name) or len(name.strip()) > 120:
            raise ValueError("Bank name must be 1 to 120 characters")
        bank = WordBank(name=name.strip(), slug=core().unique_slug(db, WordBank, name[:65]), description=description, payload={"mode": "manual"})
        db.add(bank)
        db.flush()
    now = core().utcnow().isoformat()
    result = {"bank_id": bank.id, "added": 0, "updated": 0, "skipped": 0, "failed": len(reviewed["errors"]), "definitions_added": 0, "duplicate_definitions": 0, "linked": 0, "errors": reviewed["errors"]}
    existing = {word.word.lower(): word for word in db.scalars(select(DictionaryWord).with_for_update())}
    links = {link.word.word.lower(): link for link in bank.word_links}
    for entry in reviewed["entries"]:
        word = existing.get(entry["word"])
        if word and policy == "skip":
            result["skipped"] += 1
            continue
        if not word:
            word = DictionaryWord(word=entry["word"], display_text=entry["display_text"], clue=entry["clue"], part_of_speech=entry["part_of_speech"], properties={})
            db.add(word)
            db.flush()
            existing[entry["word"]] = word
            result["added"] += 1
        else:
            result["updated"] += 1
        for field in ("display_text", "clue", "part_of_speech"):
            if entry[field] and ((policy == "update" and field in entry["supplied_fields"]) or not getattr(word, field)):
                setattr(word, field, entry[field])
        current = word.properties or {}
        incoming = {**entry["properties"], "import_source_url": entry["source_url"], "imported_at": now}
        word.properties = {**current, **incoming} if policy == "update" else {**incoming, **current}
        merge_tags(db, word, entry["tags"])
        definitions = {core().normalize_definition_signature(d.definition, d.part_of_speech): d for d in word.definitions}
        selected = None
        for index, imported in enumerate(entry["definitions"]):
            signature = core().normalize_definition_signature(imported["definition"], imported["part_of_speech"])
            definition = definitions.get(signature)
            if definition:
                result["duplicate_definitions"] += 1
            else:
                definition = DictionaryDefinition(word=word, definition=imported["definition"], part_of_speech=imported["part_of_speech"], example=text(imported.get("example")), source=(text(imported.get("source")) or text(metadata.get("source")) or "Word bank import")[:120], position=max((d.position for d in word.definitions), default=-1) + 1, properties={})
                db.add(definition)
                db.flush()
                definitions[signature] = definition
                result["definitions_added"] += 1
            if policy == "update" or not definition.properties:
                definition.properties = {**{key: entry["properties"][key] for key in ("license", "attribution", "language", "source_bank") if key in entry["properties"]}, **(definition.properties or {}), **imported.get("properties", {}), "import_source_url": entry["source_url"], "clues": imported["clues"]}
            if policy == "update" and text(imported.get("example")):
                definition.example = text(imported["example"])
            merge_tags(db, definition, imported["tags"])
            merge_tags(db, definition, [{"tag_type": "Clue", "name": clue} for clue in imported["clues"] if len(clue) <= 120])
            if imported["part_of_speech"]:
                merge_tags(db, definition, [{"tag_type": "Part of Speech", "name": imported["part_of_speech"]}])
            if index == entry["selected_definition"]:
                selected = definition
        link = links.get(entry["word"])
        if not link:
            link = WordBankWord(word=word, definition=selected, position=max((l.position for l in bank.word_links), default=-1) + 1)
            bank.word_links.append(link)
            links[entry["word"]] = link
            result["linked"] += 1
        elif policy == "update" and selected:
            link.definition = selected
    payload = dict(bank.payload or {})
    properties = dict(payload.get("properties", {}))
    incoming_metadata = {key: value for key, value in metadata.items() if key not in {"id", "slug", "name", "description", "tags", "permissions", "entries", "creator", "importer_id", "created_by_id", "created_at", "updated_at", "imports"}}
    properties = {**properties, **incoming_metadata} if policy == "update" else {**incoming_metadata, **properties}
    history = list(properties.get("imports", []))[-19:] if isinstance(properties.get("imports", []), list) else []
    history.append({"imported_at": now, "importer_id": actor.id, "sources": sorted({entry["source_url"] for entry in reviewed["entries"]}), "policy": policy, "added": result["added"], "linked": result["linked"]})
    properties.update({"imports": history, "updated_at": now, "importer_id": actor.id})
    properties.setdefault("created_by_id", actor.id)
    if metadata.get("creator"):
        properties.setdefault("origin_creator", metadata["creator"])
    if metadata.get("created_at"):
        properties.setdefault("origin_created_at", metadata["created_at"])
    payload["properties"] = properties
    bank.payload = payload
    merge_tags(db, bank, tags(metadata.get("tags")))
    db.flush()
    return result


def portable_bank(db, bank):
    entries = []
    for item in core().word_bank_entries(db, bank):
        word, selected = item["word"], item["definition"]
        definitions = [d for d in word.definitions if d.archived_at is None]
        entries.append({"word": word.word, "display_text": word.display_text, "clue": word.clue, "part_of_speech": word.part_of_speech, "tags": [{"name": t.name, "tag_type": t.tag_type} for t in word.tags], "properties": word.properties or {}, "definitions": [{"definition": d.definition, "part_of_speech": d.part_of_speech, "example": d.example, "source": d.source, "tags": [{"name": t.name, "tag_type": t.tag_type} for t in d.tags], "properties": d.properties or {}} for d in definitions], "selected_definition": definitions.index(selected) if selected in definitions else 0})
    return {"schema": "puzzary.word-bank.v1", "exported_at": core().utcnow().isoformat(), "bank": {"name": bank.name, "description": bank.description, "created_at": bank.created_at.isoformat(), "tags": [{"name": t.name, "tag_type": t.tag_type} for t in bank.tags], **(bank.payload or {}).get("properties", {})}, "entries": entries}
