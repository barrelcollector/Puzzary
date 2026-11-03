"""Shared web/API authorization. Explicit deny > allow > union of roles > deny."""
from fastapi import HTTPException
from sqlalchemy import select
from app.models import AppSetting, PermissionAudit, PermissionRole, Puzzle, PublishedIssue, User

CATALOG = {}


def register_permission(name, label, section='Plugins'):
    if name in CATALOG:
        raise ValueError(f'Permission already registered: {name}')
    CATALOG[name] = {'name': name, 'label': label, 'section': section}


for section, items in {
    'Playing': {'library.view':'Browse library', 'puzzles.play':'Play and save own progress',
                'library.archived_view':'View archived puzzles', 'library.archived_play':'Play archived puzzles',
                'puzzles.drafts.own':'View own drafts', 'puzzles.drafts.any':'View all drafts',
                'puzzles.print':'Print puzzles', 'puzzles.solutions':'View solutions',
                'puzzles.progress.any':'Submit attempts and photos for other users'},
    'Creation': {'puzzles.generate':'Generate puzzles', 'puzzles.create':'Create manual puzzles',
                 'puzzles.import':'Import puzzles', 'puzzles.export':'Export puzzles'},
    'Group Solve': {'groups.join':'Join and solve groups', 'groups.host':'Host groups',
                    'groups.invite':'Invite participants', 'groups.moderate':'Moderate own groups',
                    'groups.manage_any':'Manage any group', 'groups.guests':'Allow guest participants'},
    'Content': {'content.view':'Read dictionary and word banks', 'content.manage':'Manage dictionary, word banks and tags'},
    'Users': {'users.view':'View users', 'users.create':'Create users', 'users.edit':'Edit user profiles',
              'users.disable':'Enable or disable users', 'users.reset':'Reset passwords',
              'users.sessions':'Revoke sessions', 'users.tokens':'Manage API tokens'},
    'Server': {'roles.manage':'Manage roles and permission overrides', 'settings.manage':'Manage server settings',
               'themes.manage':'Install and manage appearance themes',
               'plugins.manage':'Manage enabled puzzle modules', 'activity.view':'Review user activity',
               'audit.view':'View permission audit log', 'api.use':'Use API tokens'},
}.items():
    for name, label in items.items():
        register_permission(name, label, section)
for resource, actions in {'puzzles': ['edit','publish','archive','delete','schedule','metadata'],
                          'issues': ['edit','publish','archive']}.items():
    for action in actions:
        for scope in ['own','any']:
            name = f'{resource}.{action}.{scope}'
            register_permission(name, f'{"Permanently delete" if action == "delete" else action.title()} {scope} {resource}', 'Publishing' if resource=='issues' else 'Puzzle management')

PLAYER = {'library.view','puzzles.play','puzzles.print','content.view','groups.join','groups.invite','api.use'}
PUBLISHER = PLAYER | {'puzzles.generate','puzzles.create','puzzles.import','puzzles.export','puzzles.drafts.own',
                       'groups.host','groups.moderate'} | {p for p in CATALOG if p.endswith('.own')}
MANAGER = PUBLISHER | {'content.manage','library.archived_view','library.archived_play','puzzles.drafts.any',
                       'activity.view','groups.manage_any'} | {p for p in CATALOG if p.endswith('.any') and p!='puzzles.progress.any'}
# Keep existing account capabilities, not new publisher or manager privileges.
LEGACY = (PLAYER - {'content.view'}) | {'puzzles.generate','puzzles.publish.own','puzzles.schedule.own','groups.host','groups.moderate'}
PRESETS = {'Player': PLAYER, 'Publisher': PUBLISHER, 'Manager': MANAGER, 'Admin': set(CATALOG), 'Existing Player': LEGACY}
ADMIN_CORE = {'roles.manage','users.edit','settings.manage'}
DANGEROUS = {'roles.manage','users.reset','users.tokens','users.disable','settings.manage','themes.manage','plugins.manage',
             'puzzles.delete.any','puzzles.delete.own','puzzles.progress.any','groups.manage_any'}


def effective_permissions(user):
    if user and getattr(user, 'is_guest_link', False):
        return set(getattr(user, 'guest_permissions', set())) & CATALOG.keys()
    if not user or not user.is_active:
        return set()
    grants = set().union(*(set(r.permissions or []) for r in user.roles)) if user.roles else set()
    if not user.permissions_initialized:
        grants |= PRESETS['Admin'] if user.is_admin else LEGACY
    overrides = user.permission_overrides or {}
    grants |= {p for p, v in overrides.items() if v is True}
    grants -= {p for p, v in overrides.items() if v is False}
    return grants & CATALOG.keys()


def management_access(user):
    permissions = effective_permissions(user)
    return bool(permissions - PLAYER - {'library.archived_view','library.archived_play','puzzles.solutions','api.use'})


def require(user, permission):
    if permission not in effective_permissions(user):
        raise HTTPException(403, f'Permission required: {permission}')


def require_owned(user, resource, action, owner_id):
    permissions = effective_permissions(user)
    if f'{resource}.{action}.any' in permissions:
        return
    if owner_id == user.id and f'{resource}.{action}.own' in permissions:
        return
    raise HTTPException(403, f'Permission required: {resource}.{action} ({"own" if owner_id == user.id else "any"})')


def can_owned(user, resource, action, owner_id):
    return user.can(f'{resource}.{action}.any') or (owner_id == user.id and user.can(f'{resource}.{action}.own'))


def can_view(user, puzzle, play=False):
    if not user or not puzzle or not user.can('library.view'):
        return False
    if (puzzle.puzzle_metadata or {}).get('draft'):
        if not (user.can('puzzles.drafts.any') or (puzzle.created_by_id == user.id and user.can('puzzles.drafts.own'))):
            return False
    elif puzzle.archived_at:
        if not user.can('library.archived_play' if play else 'library.archived_view'):
            return False
    return not play or user.can('puzzles.play')


def puzzle_is_unlisted(puzzle):
    metadata = puzzle.puzzle_metadata or {}
    return bool(metadata.get('hidden') or str(metadata.get('visibility', '')).lower() in {'hidden', 'unlisted', 'private'})


def can_discover(user, puzzle, play=False):
    return can_view(user, puzzle, play=play) and not puzzle_is_unlisted(puzzle)


def initialize_permissions(db):
    roles = {r.name:r for r in db.scalars(select(PermissionRole))}
    for name, permissions in PRESETS.items():
        if name not in roles:
            role = PermissionRole(name=name, permissions=sorted(permissions), description='Default preset' if name!='Existing Player' else 'Preserved pre-permissions access')
            db.add(role); roles[name] = role
    db.flush()
    for user in db.scalars(select(User).where(User.permissions_initialized==False)):
        user.roles = [roles['Admin' if user.is_admin else 'Existing Player']]
        user.permission_overrides = {}
        user.permissions_initialized = True
    if not db.get(AppSetting,'permission_write_lock'):
        db.add(AppSetting(key='permission_write_lock',value='1'))
    db.commit()


def lock_permissions(db):
    db.scalar(select(AppSetting).where(AppSetting.key=='permission_write_lock').with_for_update())
    db.expire_all()


def protect_last_admin(db):
    db.flush()
    if not any(ADMIN_CORE <= effective_permissions(u) for u in db.scalars(select(User).where(User.is_active==True))):
        raise HTTPException(409,'Keep at least one active administrator with role, user and server management permissions')


def audit(db, actor, subject_type, subject_id, action, before, after):
    db.add(PermissionAudit(actor_id=actor.id,subject_type=subject_type,subject_id=str(subject_id),
                           action=action,before=before,after=after))


ROUTE_PERMISSIONS = {}


def register_route_permission(endpoint_name, permission):
    if endpoint_name in ROUTE_PERMISSIONS:
        raise ValueError('Endpoint permission already registered')
    if permission not in CATALOG:
        raise ValueError('Register the plugin permission first')
    ROUTE_PERMISSIONS[endpoint_name] = permission


def authorize_request(request, db, user, administrative=False):
    route = request.scope.get('route')
    path = getattr(route,'path',request.url.path)
    name = getattr(route,'name','')
    method = request.scope.get('method','GET')
    if request.headers.get('authorization'):
        require(user,'api.use')
    if name in ROUTE_PERMISSIONS:
        require(user,ROUTE_PERMISSIONS[name]); return
    if path.startswith('/account/') or path in ['/account','/login','/logout','/password/setup']:
        return
    if path == '/api/account/stats':
        require(user, 'library.view'); return
    if path.startswith('/admin/roles') or path.endswith('/permissions') or path.endswith('/permissions/reset'):
        require(user,'roles.manage'); return
    if path.startswith('/admin/audit'):
        require(user,'audit.view'); return
    if path.startswith('/admin/users') or path in ['/api/users','/web/users']:
        permission = {'admin_create_user':'users.create','create_user':'users.create',
                      'admin_update_user':'users.edit','admin_reset_user_password':'users.reset',
                      'admin_revoke_user_sessions':'users.sessions','admin_issue_user_api_token':'users.tokens'}
        if name == 'admin_update_user' and user.can('users.disable'):
            pass
        else:
            require(user,permission.get(name,'users.view'))
        target = db.get(User,request.path_params.get('user_id')) if request.path_params.get('user_id') else None
        if target and target.id != user.id and target.can('roles.manage') and method != 'GET':
            require(user,'roles.manage')
        return
    if path.startswith('/admin/settings'):
        if not (user.can('settings.manage') or user.can('plugins.manage')):
            raise HTTPException(403,'Server or plugin management permission required')
        return
    if path.startswith('/admin/themes'):
        if not (user.can('themes.manage') or user.can('settings.manage')):
            raise HTTPException(403,'Theme management permission required')
        return
    if path.startswith('/admin/activity'):
        require(user,'activity.view'); return
    if any(path.startswith(p) for p in ['/admin/dictionary','/admin/dictionaries','/admin/word-banks','/admin/tags','/admin/categories','/admin/taxonomy','/api/dictionary','/api/word-banks','/api/tags']):
        require(user,'content.manage' if path.startswith('/admin/') or method!='GET' else 'content.view'); return
    if path.startswith('/api/groups') or path.endswith('/groups') or name=='presence_socket':
        permission = 'groups.host' if name=='create_group' else 'groups.invite' if name=='group_invitation' else 'groups.join'
        if name in ['manage_member','transfer_host','close_group','group_settings','group_puzzles','move_group']:
            permission = 'groups.moderate'
            from app.groups import group_for
            group = group_for(db,request.path_params['group_id'])
            if group.host_id != user.id:
                permission = 'groups.manage_any'
        require(user,permission)
        require(user,'puzzles.play')
        puzzle_id = request.path_params.get('puzzle_id')
        group_id = request.path_params.get('group_id')
        if group_id:
            from app.groups import group_for
            puzzle_id = group_for(db, group_id).puzzle_id
        if puzzle_id and not can_view(user, db.get(Puzzle,puzzle_id), play=True):
            raise HTTPException(403,'Puzzle access denied')
        return
    if path.startswith('/admin/publishing') or path.startswith('/api/issues') or path.startswith('/issues/'):
        if name in ['api_issue_list','api_latest_issue','api_latest_issue_pdf','api_get_issue','api_issue_pdf','web_issue_preview','web_issue_pdf']:
            require(user,'puzzles.print' if 'pdf' in name else 'library.view'); return
        if name=='admin_publishing_page':
            if not any(p.startswith('issues.') for p in effective_permissions(user)): raise HTTPException(403,'Publishing permission required')
            return
        issue_id = request.path_params.get('issue_id')
        issue = db.get(PublishedIssue,issue_id) if issue_id else None
        action = request.path_params.get('action','edit')
        action = 'publish' if name=='api_publish_issue' or action=='publish' else 'archive' if action in ['archive','restore'] else 'edit'
        if issue: require_owned(user,'issues',action,issue.created_by_id)
        else: require(user,'issues.edit.own')
        return
    if path.startswith('/admin/series'):
        if not (user.can('puzzles.metadata.own') or user.can('puzzles.metadata.any')):
            raise HTTPException(403, 'Puzzle metadata permission required')
        return
    if path=='/admin':
        if not management_access(user): raise HTTPException(403,'Management access required')
        return
    if path in ['/generate','/web/puzzles/generate','/admin/generate','/api/puzzles/generate','/api/puzzle-types']:
        require(user,'puzzles.generate'); return
    if path=='/admin/puzzles':
        if not any(p.startswith('puzzles.') and (p.endswith('.own') or p.endswith('.any') or p in ['puzzles.create','puzzles.import']) for p in effective_permissions(user)):
            raise HTTPException(403,'Puzzle management permission required')
        return
    if path in ['/admin/puzzles/manual','/api/puzzles/manual']:
        require(user,'puzzles.create'); return
    if path in ['/admin/puzzles/import','/api/puzzles/import']:
        require(user,'puzzles.import'); return
    if path=='/api/puzzles/export':
        require(user,'puzzles.export'); return
    puzzle_id = request.path_params.get('puzzle_id')
    if puzzle_id:
        puzzle = db.get(Puzzle,puzzle_id)
        if not puzzle: raise HTTPException(404,'Puzzle not found')
        action = path.rsplit('/',1)[-1]
        scoped = {'edit':'edit','manual':'edit','publish':'publish','archive':'archive','unarchive':'archive','delete':'delete','metadata':'metadata'}
        if path.startswith('/admin/') or (path.endswith('/manual') and method!='GET'):
            require_owned(user,'puzzles',scoped.get(action,'edit'),puzzle.created_by_id); return
        require(user,'puzzles.export' if action=='export' else 'puzzles.print' if 'print' in path else 'puzzles.play' if action in ['progress','reset','attempts','photos','actions','activity'] else 'library.view')
        if not can_view(user,puzzle,play=action in ['progress','reset','attempts','photos','actions','activity'] or name=='puzzle_page'):
            raise HTTPException(403,'Puzzle access denied')
        return
    if path in ['/','/library','/api/puzzles','/api/puzzles/latest']:
        require(user,'library.view'); return
    raise HTTPException(403,'No permission contract registered for this endpoint')
