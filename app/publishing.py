"""Saved drafts and immutable printable issues, independent of source puzzle lifetime."""

from copy import deepcopy
from html.parser import HTMLParser
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PublishedIssue, Puzzle
from app.print_pdf import render_pdf
from app.booklet import booklet_layout, impose_booklet
from app.permissions import can_owned, require_owned, can_view

router = APIRouter()


def core():
    # The shared authentication and module print renderer live in the application entrypoint.
    from app import main
    return main


class IssueModule(BaseModel):
    kind: Literal["puzzle", "text"] = "puzzle"
    puzzle_id: str = Field(default="", max_length=26)
    title: str = Field(default="", max_length=160)
    body: str = Field(default="", max_length=10000)
    attribution: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_content(self):
        if self.kind == "puzzle" and not self.puzzle_id.strip():
            raise ValueError("Select a puzzle for every puzzle section")
        if self.kind == "text" and (not self.title.strip() or not self.body.strip()):
            raise ValueError("Text sections need a title and content")
        return self


class IssueWrite(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    modules: list[IssueModule] = Field(default_factory=list, max_length=20)


class SheetExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "article" and (self.depth or "print-sheet" in dict(attrs).get("class", "").split()):
            self.depth += 1
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.depth:
            self.parts.append(f"</{tag}>")
            if tag == "article":
                self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_entityref(self, name):
        if self.depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.depth:
            self.parts.append(f"&#{name};")


def find_issue(db, issue_id, lock=False):
    statement = select(PublishedIssue).where(PublishedIssue.id == issue_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    issue = db.scalar(statement)
    if issue is None:
        raise HTTPException(404, "Issue not found")
    return issue


def require_issue_access(issue, user):
    if not can_owned(user,'issues','edit',issue.created_by_id) and (issue.published_at is None or issue.archived_at is not None):
        raise HTTPException(404, "Issue not found")


def validate_puzzle(db, puzzle_id, publishing=False):
    puzzle = db.get(Puzzle, puzzle_id)
    if puzzle is None:
        raise HTTPException(422, f"Puzzle {puzzle_id} no longer exists")
    plugin = core().get_puzzle_type(puzzle.puzzle_type)
    if plugin is None or not plugin.supports_print or plugin.digital_only:
        raise HTTPException(422, f"{puzzle.title} does not support printing")
    if publishing and (puzzle.puzzle_metadata or {}).get("draft"):
        raise HTTPException(422, f"Publish the puzzle '{puzzle.title}' before publishing this issue")
    return puzzle


def write_issue(db, user, payload, issue=None):
    require_owned(user,'issues','edit',issue.created_by_id if issue else user.id)
    if issue is not None and issue.published_at is not None:
        raise HTTPException(409, "Published issues are immutable; duplicate this issue to make changes")
    if not payload.title.strip():
        raise HTTPException(422, "An issue title is required")
    for item in payload.modules:
        if item.kind == "puzzle":
            puzzle = validate_puzzle(db, item.puzzle_id)
            if not can_view(user,puzzle): raise HTTPException(403,'Puzzle access denied')
    if issue is None:
        issue = PublishedIssue(id=uuid.uuid4().hex[:12], created_by_id=user.id)
        db.add(issue)
    issue.title = payload.title.strip()
    issue.description = payload.description.strip()
    issue.modules = [item.model_dump() for item in payload.modules]
    db.commit()
    db.refresh(issue)
    return issue


def snapshot_modules(issue, request, db, publishing=False):
    c = core()
    entries = []
    for module in issue.modules:
        if module["kind"] == "puzzle":
            puzzle = validate_puzzle(db, module["puzzle_id"], publishing=publishing)
            response = c.render_puzzle_print_page(puzzle.id, request, db, None, False, pdf_mode=True)
            extractor = SheetExtractor()
            extractor.feed(response.body.decode("utf-8"))
            if not extractor.parts:
                raise HTTPException(422, "Puzzle print renderer did not produce a print sheet")
            entries.append({"kind": "puzzle", "puzzle_id": puzzle.id, "title": puzzle.title,
                            "puzzle_type": puzzle.puzzle_type, "print_payload": deepcopy(puzzle.print_payload),
                            "html": "".join(extractor.parts)})
        else:
            html = c.templates.env.get_template("issue_text.html").render(module=module)
            entries.append({**module, "html": html})
    return {"schema": "puzzary.issue.v1", "css": (c.BASE_DIR / "static" / "styles.css").read_text(encoding="utf-8"),
            "modules": entries}


def publish_issue(db, issue, request, user):
    require_owned(user,'issues','publish',issue.created_by_id)
    if issue.published_at is not None:
        return issue
    if issue.archived_at is not None:
        raise HTTPException(409, "Restore the draft before publishing")
    if not issue.modules:
        raise HTTPException(422, "Add at least one section before publishing")
    issue.snapshot = snapshot_modules(issue, request, db, publishing=True)
    issue.published_at = core().utcnow()
    db.commit()
    db.refresh(issue)
    return issue


def issue_metadata(issue, db):
    base = core().app_base_url(db)
    modules = issue.snapshot.get("modules", []) if issue.published_at else issue.modules
    return {"id": issue.id, "title": issue.title, "description": issue.description,
            "status": "published" if issue.published_at else "draft", "archived": issue.archived_at is not None,
            "created_at": issue.created_at, "published_at": issue.published_at,
            "modules": [{k: v for k, v in item.items() if k != "html"} for item in modules],
            "url": f"{base}/issues/{issue.id}", "pdf_url": f"{base}/api/issues/{issue.id}/print.pdf"}


def print_issue(issue, request, db, user, toolbar=True):
    c = core()
    snapshot = issue.snapshot if issue.published_at else snapshot_modules(issue, request, db)
    print_mode = request.query_params.get("print_mode", "standard")
    if print_mode not in {"standard", "booklet"}:
        raise HTTPException(422, "Print layout must be standard or booklet")
    sheet = c.apply_print_request_layout({"page_size": "letter"} if print_mode == "booklet" else {}, request)
    try:
        layout = booklet_layout(sheet) if print_mode == "booklet" else sheet
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return c.templates.TemplateResponse("issue_print.html", {
        "request": request, "issue": issue, "snapshot": snapshot, "print_layout": layout,
        "print_pdf_css": snapshot["css"], "current_user": user,
        "hide_app_chrome": True, "show_print_toolbar": toolbar,
        "sheet_layout": sheet, "print_mode": print_mode,
    })


def issue_pdf(issue, request, db, user):
    rendered = print_issue(issue, request, db, user, toolbar=False)
    try:
        data = render_pdf(rendered.body.decode(), rendered.context["print_layout"])
        if rendered.context["print_mode"] == "booklet":
            data = impose_booklet(data, rendered.context["sheet_layout"])
    except TimeoutError as exc:
        raise HTTPException(503, str(exc), headers={"Retry-After": "10"}) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PlaywrightError as exc:
        raise HTTPException(503, "PDF rendering failed. Please retry.") from exc
    suffix = "-booklet" if rendered.context["print_mode"] == "booklet" else ""
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="puzzary-issue-{issue.id}{suffix}.pdf"',
        "Cache-Control": "private, no-store",
    })


@router.get("/admin/publishing/{issue_id}", response_class=HTMLResponse)
def admin_issue_editor(issue_id: str, request: Request, db: Session = Depends(get_db)):
    c = core()
    user = c.require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    context = c.admin_context(request, db, user, "publishing")
    context["selected_issue"] = find_issue(db, issue_id)
    return c.templates.TemplateResponse("admin.html", context)


@router.post("/admin/publishing/save")
def web_issue_save(request: Request, issue_id: str = Form(""), title: str = Form(...),
                   description: str = Form(""), modules_json: str = Form("[]"), action: str = Form("save"), db: Session = Depends(get_db)):
    c = core()
    user = c.require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        import json
        payload = IssueWrite(title=title, description=description, modules=json.loads(modules_json))
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(422, "Invalid issue sections or missing content") from exc
    existing = find_issue(db, issue_id, lock=True) if issue_id else None
    if action == 'publish': require_owned(user,'issues','publish',existing.created_by_id if existing else user.id)
    issue = write_issue(db, user, payload, existing)
    if action == "publish":
        issue = publish_issue(db, find_issue(db, issue.id, lock=True), request, user)
    if action == "preview":
        return RedirectResponse(f"/issues/{issue.id}", status_code=303)
    return RedirectResponse(f"/admin/publishing/{issue.id}", status_code=303)


@router.post("/admin/publishing/{issue_id}/{action}")
def web_issue_action(issue_id: str, action: str, request: Request, db: Session = Depends(get_db)):
    c = core()
    user = c.require_admin_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    issue = find_issue(db, issue_id, lock=True)
    if action == "publish":
        publish_issue(db, issue, request, user)
    elif action in ("archive", "restore"):
        issue.archived_at = c.utcnow() if action == "archive" else None
        db.commit()
    elif action == "duplicate":
        issue = write_issue(db, user, IssueWrite(title=(issue.title[:150] + " (copy)"),
                                               description=issue.description, modules=issue.modules))
    else:
        raise HTTPException(404, "Unknown issue action")
    return RedirectResponse(f"/admin/publishing/{issue.id}", status_code=303)


@router.get("/issues/{issue_id}", response_class=HTMLResponse)
def web_issue_preview(issue_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    issue = find_issue(db, issue_id)
    require_issue_access(issue, user)
    return print_issue(issue, request, db, user)


@router.get("/issues/{issue_id}/print.pdf")
def web_issue_pdf(issue_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_web_user(request, db)
    if isinstance(user, RedirectResponse):
        return user
    issue = find_issue(db, issue_id)
    require_issue_access(issue, user)
    return issue_pdf(issue, request, db, user)


@router.get("/api/issues")
def api_issue_list(request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    statement = select(PublishedIssue).where(PublishedIssue.archived_at.is_(None)).order_by(desc(PublishedIssue.created_at)).limit(100)
    return [issue_metadata(issue, db) for issue in db.scalars(statement) if issue.published_at is not None or can_owned(user,'issues','edit',issue.created_by_id)]


@router.get("/api/issues/latest")
def api_latest_issue(request: Request, db: Session = Depends(get_db)):
    core().require_api_user(request, db)
    return issue_metadata(latest_published_issue(db), db)


def latest_published_issue(db):
    issue = db.scalar(select(PublishedIssue).where(PublishedIssue.published_at.is_not(None), PublishedIssue.archived_at.is_(None))
                      .order_by(desc(PublishedIssue.published_at)).limit(1))
    if issue is None:
        raise HTTPException(404, "No published issues")
    return issue


@router.get("/api/issues/latest/print.pdf")
def api_latest_issue_pdf(request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    return issue_pdf(latest_published_issue(db), request, db, user)


@router.post("/api/issues", status_code=201)
def api_create_issue(payload: IssueWrite, request: Request, db: Session = Depends(get_db)):
    user = core().require_admin_api(request, db)
    return issue_metadata(write_issue(db, user, payload), db)


@router.put("/api/issues/{issue_id}")
def api_update_issue(issue_id: str, payload: IssueWrite, request: Request, db: Session = Depends(get_db)):
    user = core().require_admin_api(request, db)
    return issue_metadata(write_issue(db, user, payload, find_issue(db, issue_id, lock=True)), db)


@router.post("/api/issues/{issue_id}/publish")
def api_publish_issue(issue_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_admin_api(request, db)
    return issue_metadata(publish_issue(db, find_issue(db, issue_id, lock=True), request, user), db)


@router.get("/api/issues/{issue_id}")
def api_get_issue(issue_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    issue = find_issue(db, issue_id)
    require_issue_access(issue, user)
    return issue_metadata(issue, db)


@router.get("/api/issues/{issue_id}/print.pdf")
def api_issue_pdf(issue_id: str, request: Request, db: Session = Depends(get_db)):
    user = core().require_api_user(request, db)
    issue = find_issue(db, issue_id)
    require_issue_access(issue, user)
    return issue_pdf(issue, request, db, user)
