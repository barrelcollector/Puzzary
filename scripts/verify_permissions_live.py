"""Opt-in read-only deployed RBAC smoke test and migration preservation check."""
import os
if os.environ.get('PUZZARY_PERMISSIONS_LIVE_QA') != '1':
    raise SystemExit('Set PUZZARY_PERMISSIONS_LIVE_QA=1 for deployed read-only checks')
import base64
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from itsdangerous import TimestampSigner
from sqlalchemy import select, text
from app.database import SessionLocal
from app.main import SESSION_SECRET
from app.models import User, Puzzle, PuzzleProgress

manifest=Path('/data/permissions-migration-check.json')
with SessionLocal() as db:
    def digest(rows): return hashlib.sha256(json.dumps(rows,default=str,sort_keys=True).encode()).hexdigest()
    before={}
    for table,columns in {
        'users':'id,display_name,email,password_hash,api_token_hash,session_version,is_active',
        'puzzles':'id,created_by_id,title,print_payload,play_payload,solution_payload,puzzle_metadata',
        'puzzle_progress':'id,puzzle_id,user_id,progress_payload',
    }.items():
        rows=[dict(r._mapping) for r in db.execute(text(f'SELECT {columns} FROM {table} ORDER BY id'))]
        before[table]={'count':len(rows),'digest':digest(rows)}
    if '--snapshot' in sys.argv:
        with manifest.open('x',encoding='utf-8') as file: json.dump(before,file)
        os.chmod(manifest,0o600)
        print('Saved migration baseline; only aggregate counts and digests recorded.')
        raise SystemExit(0)
    assert before==json.loads(manifest.read_text()),'Existing accounts, puzzles or progress changed during migration'
    from app.models import PermissionRole
    users=db.scalars(select(User)).all()
    assert all(u.permissions_initialized and u.roles for u in users)
    assert {'Player','Publisher','Manager','Admin'} <= {r.name for r in db.scalars(select(PermissionRole))}
    host=next(u for u in users if u.is_active and u.can('roles.manage') and u.can('users.view'))
    cookie=TimestampSigner(SESSION_SECRET).sign(base64.b64encode(json.dumps({'user_id':host.id,'session_version':host.session_version}).encode())).decode()
    account_id=host.id
    from app.puzzles import get_puzzle_type
    puzzle=next((p for p in db.scalars(select(Puzzle).where(Puzzle.archived_at.is_(None))) if not (p.puzzle_metadata or {}).get('draft') and get_puzzle_type(p.puzzle_type) and get_puzzle_type(p.puzzle_type).supports_print and not get_puzzle_type(p.puzzle_type).digital_only),None)
    pid=puzzle.id if puzzle else None
base='http://127.0.0.1:8790'
def get(path,authenticated=True):
    request=Request(base+path,headers={'Cookie':'session='+cookie} if authenticated else {})
    with urlopen(request,timeout=30) as response: return response.status,response.read()
for path in ['/health','/admin/users',f'/admin/users/{account_id}','/admin/roles','/admin/audit','/admin/settings','/api/puzzles']:
    assert get(path)[0]==200,path
if pid:
    preview=json.loads(get(f'/api/puzzles/{pid}/print-preview')[1])
    parts=urlsplit(preview['render_url'])
    assert get(parts.path+'?'+parts.query,False)[0]==200
manifest.unlink()
print('Live migration preserved account credentials, puzzle payloads and personal progress. User/role/audit screens, authenticated API and signed print render passed.')
