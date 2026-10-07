"""Account-owned, short-lived word-bank import previews and portable exports."""
import json
import secrets
import threading
import time
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from app.database import get_db
from app.models import WordBank
from app.puzzles import list_puzzle_types
from app.word_bank_imports import FIELDS, commit_import, parse_sources, portable_bank, preview, validate_structure
from app.word_bank_sources import MAX_BYTES, fetch_sources

router = APIRouter()
PREVIEWS = {}
LOCK = threading.RLock()
TTL = 15 * 60


def core():
    from app import main
    return main


def actor_for(request, db):
    return core().require_api_user(request, db) if request.url.path.startswith("/api/") else core().require_admin_user(request, db)


def remember(actor, parsed):
    with LOCK:
        for key, value in list(PREVIEWS.items()):
            if value["expires"] < time.monotonic():
                del PREVIEWS[key]
        if len(PREVIEWS) >= 32 or sum(value["actor"] == actor.id for value in PREVIEWS.values()) >= 4:
            raise ValueError("Too many open imports; finish one or wait 15 minutes")
        key = secrets.token_urlsafe(24)
        PREVIEWS[key] = {"actor": actor.id, "expires": time.monotonic() + TTL, "parsed": parsed}
        return key


def get_preview(actor, key):
    item = PREVIEWS.get(key)
    if not item or item["actor"] != actor.id or item["expires"] < time.monotonic():
        raise HTTPException(404, "Import preview expired or is unavailable; start again")
    return item


def render(request, db, actor, **context):
    return core().templates.TemplateResponse("word_bank_import.html", {"request": request, "current_user": actor, "fields": FIELDS, "banks": db.scalars(select(WordBank).where(WordBank.archived_at.is_(None)).order_by(WordBank.name)).all(), "plugins": [plugin for plugin in list_puzzle_types() if "word_bank" in plugin.source_requirements], "stage": "source", **context})


def safe_error(error):
    return str(error) if isinstance(error, (ValueError, json.JSONDecodeError)) else "Source could not be read; check its format, availability and authorization"


def metadata_from(values, original):
    metadata = dict(original)
    for key in ("source", "license", "language", "attribution"):
        value = str(values.get(key, "")).strip()
        if len(value) > 2000:
            raise ValueError(f"{key.title()} is too long")
        if value:
            metadata[key] = value
    validate_structure(metadata)
    return metadata


def validate_target(values):
    bank_id = values.get("bank_id")
    try:
        bank_id = int(bank_id) if bank_id else None
    except (ValueError, TypeError):
        raise ValueError("Invalid destination bank")
    name = str(values.get("name", "")).strip()
    description = str(values.get("description", "")).strip()
    if not bank_id and (not name or len(name) > 120):
        raise ValueError("Give the new bank a name, up to 120 characters")
    if len(description) > 10000:
        raise ValueError("Description is too long")
    return bank_id, name, description


@router.get("/admin/word-banks/import")
def import_page(request: Request, db=Depends(get_db)):
    actor = actor_for(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    return render(request, db, actor, target_bank=request.query_params.get("bank_id", ""))


@router.post("/admin/word-banks/import/source")
async def import_source(request: Request, db=Depends(get_db)):
    actor = actor_for(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    form = await request.form()
    try:
        sources = []
        upload = form.get("file")
        if upload and hasattr(upload, "read") and upload.filename:
            content = await upload.read(MAX_BYTES + 1)
            await upload.close()
            if len(content) > MAX_BYTES:
                raise ValueError("File exceeds the 2 MB limit")
            sources.append({"text": content.decode("utf-8-sig"), "url": upload.filename[:255], "content_type": upload.content_type or ""})
        pasted = str(form.get("source_text", ""))
        if pasted.strip():
            sources.append({"text": pasted, "url": "pasted-content", "content_type": ""})
        source_url = str(form.get("source_url", "")).strip()
        if source_url:
            sources.extend(await run_in_threadpool(fetch_sources, source_url, str(form.get("api_token", ""))))
        parsed = parse_sources(sources, str(form.get("format", "auto")))
        key = remember(actor, parsed)
        return render(request, db, actor, stage="mapping", parsed=parsed, preview_id=key, target_bank=form.get("bank_id", ""))
    except (ValueError, OSError, KeyError) as error:
        return render(request, db, actor, error=safe_error(error), target_bank=form.get("bank_id", ""))


@router.get("/admin/word-banks/import/fields")
def import_fields(request: Request, db=Depends(get_db)):
    actor = actor_for(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    key = request.query_params.get("preview_id", "")
    with LOCK:
        state = get_preview(actor, key)
        parsed = {**state["parsed"], "metadata": {**state["parsed"]["metadata"], **state.get("metadata", {}), "name": state.get("name", state["parsed"]["metadata"].get("name", "")), "description": state.get("description", "")}}
        return render(request, db, actor, stage="mapping", parsed=parsed, preview_id=key, target_bank=state.get("reviewed", {}).get("bank_id", ""))


@router.post("/admin/word-banks/import/preview")
async def import_preview(request: Request, db=Depends(get_db)):
    actor = actor_for(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    form = await request.form()
    key = str(form.get("preview_id", ""))
    with LOCK:
        state = get_preview(actor, key)
        try:
            bank_id, name, description = validate_target(form)
            mapping = {field: str(form.get(f"map_{field}", "")) for field in FIELDS}
            reviewed = preview(db, state["parsed"], mapping, bank_id, str(form.get("plugin", "")))
            metadata = metadata_from(form, state["parsed"]["metadata"])
            state["parsed"]["mapping"] = mapping
            state.update({"reviewed": reviewed, "name": name, "description": description, "metadata": metadata})
            return render(request, db, actor, stage="preview", reviewed=reviewed, preview_id=key, name=name or db.get(WordBank, bank_id).name)
        except ValueError as error:
            return render(request, db, actor, stage="mapping", parsed=state["parsed"], preview_id=key, error=str(error), target_bank=form.get("bank_id", ""))


def finish_import(db, actor, key, policy, allow_invalid):
    with LOCK:
        state = get_preview(actor, key)
        if not state.get("reviewed"):
            raise ValueError("Preview the mapped entries first")
        if state["reviewed"]["errors"] and not allow_invalid:
            raise ValueError("Fix invalid rows or explicitly skip them")
        try:
            result = commit_import(db, actor, state["reviewed"], state["name"], state["description"], state["metadata"], policy)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ValueError("A concurrent import changed these words; preview the source again")
        except Exception:
            db.rollback()
            raise
        del PREVIEWS[key]
        return result


@router.post("/admin/word-banks/import/commit")
async def import_commit(request: Request, db=Depends(get_db)):
    actor = actor_for(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    form = await request.form()
    key = str(form.get("preview_id", ""))
    try:
        result = finish_import(db, actor, key, str(form.get("policy", "preserve")), form.get("skip_invalid") == "yes")
        return render(request, db, actor, stage="summary", result=result)
    except ValueError as error:
        state = get_preview(actor, key)
        return render(request, db, actor, stage="preview", reviewed=state["reviewed"], preview_id=key, name=state["name"], error=str(error))


async def api_body(request):
    raw = await request.body()
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "Request exceeds the 2 MB limit")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise HTTPException(422, "Request must contain valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(422, "Request must be a JSON object")
    return payload


@router.post("/api/word-banks/import/preview")
async def api_import_preview(request: Request, db=Depends(get_db)):
    actor = actor_for(request, db)
    payload = await api_body(request)
    try:
        sources = [{"text": str(payload["source_text"]), "url": "pasted-content", "content_type": ""}] if payload.get("source_text") else []
        if payload.get("source_url"):
            sources.extend(await run_in_threadpool(fetch_sources, str(payload["source_url"]), str(payload.get("api_token", ""))))
        parsed = parse_sources(sources, str(payload.get("format", "auto")))
        bank_id, name, description = validate_target({**parsed["metadata"], **payload})
        mapping = payload.get("mapping", parsed["mapping"])
        if not isinstance(mapping, dict):
            raise ValueError("Mapping must be an object")
        reviewed = preview(db, parsed, mapping, bank_id, str(payload.get("plugin", "")))
        key = remember(actor, parsed)
        with LOCK:
            PREVIEWS[key].update({"reviewed": reviewed, "name": name, "description": description, "metadata": metadata_from(payload, parsed["metadata"])})
        return {"preview_id": key, "columns": parsed["columns"], "mapping": mapping, **reviewed}
    except (ValueError, OSError, KeyError) as error:
        raise HTTPException(422, safe_error(error))


@router.post("/api/word-banks/import/commit")
async def api_import_commit(request: Request, db=Depends(get_db)):
    actor = actor_for(request, db)
    payload = await api_body(request)
    try:
        return finish_import(db, actor, str(payload.get("preview_id", "")), str(payload.get("policy", "preserve")), payload.get("skip_invalid") is True)
    except ValueError as error:
        raise HTTPException(422, str(error))


@router.get("/api/word-banks/export")
def api_export_banks(request: Request, db=Depends(get_db)):
    actor_for(request, db)
    banks = db.scalars(select(WordBank).where(WordBank.archived_at.is_(None)).order_by(WordBank.name)).all()
    return {"schema": "puzzary.word-bank.bundle.v1", "banks": [portable_bank(db, bank) for bank in banks]}


@router.get("/api/word-banks/{bank_id}/export")
@router.get("/admin/word-banks/{bank_id}/export")
def export_bank(bank_id: int, request: Request, db=Depends(get_db)):
    actor = actor_for(request, db)
    if isinstance(actor, RedirectResponse):
        return actor
    bank = db.get(WordBank, bank_id)
    if not bank:
        raise HTTPException(404, "Word bank not found")
    return Response(json.dumps(portable_bank(db, bank), ensure_ascii=False), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="puzzary-word-bank-{bank.id}.json"'})
