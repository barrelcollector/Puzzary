"""Role and per-account permission administration with serialized, audited writes."""
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from app.database import get_db
from app.models import PermissionRole, PermissionAudit, User
from app.permissions import CATALOG, DANGEROUS, ADMIN_CORE, effective_permissions, require, lock_permissions, protect_last_admin, audit

router = APIRouter()


def core():
    from app import main
    return main


def groups():
    result = defaultdict(list)
    for item in CATALOG.values():
        result[item['section']].append(item)
    return dict(result)


def state(user):
    return {'roles': sorted(r.id for r in user.roles), 'overrides': user.permission_overrides or {}}


def finish(db):
    protect_last_admin(db)
    for user in db.scalars(select(User)):
        user.is_admin = ADMIN_CORE <= effective_permissions(user)
    db.commit()


@router.get('/admin/users/{user_id}')
def permission_user_page(user_id: int, request: Request, db=Depends(get_db)):
    actor = core().require_admin_user(request, db)
    if isinstance(actor, RedirectResponse): return actor
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, 'User not found')
    inherited = set().union(*(set(r.permissions or []) for r in user.roles)) if user.roles else set()
    events = db.scalars(select(PermissionAudit).where(PermissionAudit.subject_type=='user', PermissionAudit.subject_id==str(user.id)).order_by(PermissionAudit.id.desc()).limit(30)).all() if actor.can('audit.view') else []
    return core().templates.TemplateResponse('permission_admin.html', {
        'request':request, 'current_user':actor, 'section':'user_detail', 'managed_user':user,
        'roles':db.scalars(select(PermissionRole).order_by(PermissionRole.name)).all(),
        'permission_groups':groups(), 'inherited':inherited, 'effective':effective_permissions(user),
        'dangerous':DANGEROUS, 'events':events,
    })


@router.post('/admin/users/{user_id}/permissions')
@router.post('/admin/users/{user_id}/permissions/reset')
async def save_user_permissions(user_id: int, request: Request, db=Depends(get_db)):
    actor = core().require_admin_user(request, db)
    if isinstance(actor, RedirectResponse): return actor
    lock_permissions(db); require(actor, 'roles.manage')
    user = db.get(User, user_id)
    if not user: raise HTTPException(404, 'User not found')
    before = state(user)
    if request.url.path.endswith('/reset'):
        user.permission_overrides = {}
    else:
        form = await request.form()
        try: ids = {int(v) for v in form.getlist('roles')}
        except ValueError: raise HTTPException(422, 'Invalid role')
        roles = db.scalars(select(PermissionRole).where(PermissionRole.id.in_(ids))).all()
        if len(roles) != len(ids): raise HTTPException(422, 'Unknown role')
        overrides = {}
        for key, value in form.multi_items():
            if not key.startswith('permission:'): continue
            name = key.removeprefix('permission:')
            if name not in CATALOG or value not in ['inherit','allow','deny']:
                raise HTTPException(422, 'Invalid permission override')
            if value != 'inherit': overrides[name] = value == 'allow'
        user.roles = roles
        user.permission_overrides = overrides
    user.permissions_initialized = True
    audit(db, actor, 'user', user.id, 'permissions.updated', before, state(user))
    finish(db)
    return RedirectResponse(f'/admin/users/{user.id}', status_code=303)


@router.get('/admin/roles')
def roles_page(request: Request, db=Depends(get_db)):
    actor = core().require_admin_user(request, db)
    if isinstance(actor, RedirectResponse): return actor
    return core().templates.TemplateResponse('permission_admin.html', {
        'request':request, 'current_user':actor, 'section':'roles',
        'roles':db.scalars(select(PermissionRole).order_by(PermissionRole.name)).all(),
        'permission_groups':groups(), 'dangerous':DANGEROUS,
    })


@router.post('/admin/roles')
@router.post('/admin/roles/{role_id}')
async def save_role(request: Request, role_id: int | None = None, db=Depends(get_db)):
    actor = core().require_admin_user(request, db)
    if isinstance(actor, RedirectResponse): return actor
    lock_permissions(db); require(actor, 'roles.manage')
    form = await request.form()
    name = str(form.get('name','')).strip()
    if not name or len(name)>80: raise HTTPException(422, 'Role name must be 1 to 80 characters')
    role = db.get(PermissionRole, role_id) if role_id else None
    if role_id and not role: raise HTTPException(404, 'Role not found')
    existing = db.scalar(select(PermissionRole).where(PermissionRole.name == name))
    if existing and existing is not role: raise HTTPException(409, 'Role name already exists')
    permissions = sorted(set(form.getlist('permissions')))
    if any(p not in CATALOG for p in permissions): raise HTTPException(422, 'Unknown permission')
    before = {'name':role.name,'permissions':role.permissions} if role else {}
    if not role:
        role = PermissionRole(name=name); db.add(role)
    role.name = name
    role.description = str(form.get('description',''))[:255]
    role.permissions = permissions
    db.flush()
    audit(db, actor, 'role', role.id, 'role.updated', before, {'name':name,'permissions':permissions})
    finish(db)
    return RedirectResponse('/admin/roles', status_code=303)


@router.get('/admin/audit')
def audit_page(request: Request, db=Depends(get_db)):
    actor = core().require_admin_user(request, db)
    if isinstance(actor, RedirectResponse): return actor
    events = db.scalars(select(PermissionAudit).order_by(PermissionAudit.id.desc()).limit(200)).all()
    return core().templates.TemplateResponse('permission_admin.html', {
        'request':request, 'current_user':actor, 'section':'audit', 'events':events,
        'actors':{u.id:u.display_name for u in db.scalars(select(User))},
    })
