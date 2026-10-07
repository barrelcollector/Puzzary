"""Verify populated legacy Group Solve tables migrate without losing progress."""
import os
os.environ['DATABASE_URL'] = 'sqlite:////tmp/puzzary-guest-migration-qa.sqlite'
from datetime import datetime, timedelta, timezone
from sqlalchemy import text, inspect
from app.database import Base, engine, SessionLocal, ensure_schema
from app.models import User, Puzzle, GroupSolve, GroupMember, GroupAction

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
with SessionLocal() as db:
    user = User(display_name='Migration host', is_admin=True)
    puzzle = Puzzle(id='migration-puzzle', puzzle_type='takuzu', title='Migration QA', theme='', difficulty='easy',
                    play_payload={}, print_payload={}, solution_payload={})
    db.add_all([user, puzzle]); db.flush()
    db.add(GroupSolve(id='migration-group', code='MIGRATION', puzzle_id=puzzle.id, host_id=user.id,
                      state={'answers': {'0,0': '1'}}, revision=3,
                      expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
    db.commit()
    uid = user.id
with engine.begin() as conn:
    conn.execute(text('DROP TABLE group_actions'))
    conn.execute(text('DROP TABLE group_members'))
    conn.execute(text('ALTER TABLE group_solves DROP COLUMN allow_guests'))
    conn.execute(text('ALTER TABLE group_solves DROP COLUMN puzzle_states'))
    conn.execute(text('ALTER TABLE group_solves DROP COLUMN puzzle_started_revision'))
    conn.execute(text('''CREATE TABLE group_members (
        id INTEGER PRIMARY KEY, group_id VARCHAR(26) NOT NULL REFERENCES group_solves(id),
        user_id INTEGER NOT NULL REFERENCES users(id), status VARCHAR(16) NOT NULL,
        focus VARCHAR(80) NOT NULL, presence JSON, seen_at TIMESTAMP,
        CONSTRAINT uq_group_member UNIQUE (group_id, user_id))'''))
    conn.execute(text('''CREATE TABLE group_actions (
        id INTEGER PRIMARY KEY, group_id VARCHAR(26) NOT NULL REFERENCES group_solves(id),
        action_id VARCHAR(36) NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
        revision INTEGER NOT NULL, kind VARCHAR(16) NOT NULL, changes JSON NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT uq_group_action UNIQUE (group_id, action_id))'''))
    conn.execute(text("INSERT INTO group_members (id,group_id,user_id,status,focus) VALUES (1,'migration-group',:uid,'active','0,0')"), {'uid': uid})
    conn.execute(text("INSERT INTO group_actions (id,group_id,action_id,user_id,revision,kind,changes) VALUES (1,'migration-group','migration-action',:uid,3,'patch',:changes)"), {'uid': uid, 'changes': '{"0,0":"1"}'})
ensure_schema()
ensure_schema()
with SessionLocal() as db:
    group = db.get(GroupSolve, 'migration-group')
    assert not group.allow_guests and group.revision == 3 and group.state['answers']['0,0'] == '1'
    assert group.puzzle_states == {} and group.puzzle_started_revision == 0
    assert db.get(GroupMember, 1).user_id == uid
    assert db.get(GroupAction, 1).changes == {'0,0': '1'}
    member = GroupMember(group_id=group.id, guest_name='Migration guest', guest_token_hash='a' * 64)
    db.add(member); db.flush()
    db.add(GroupAction(group_id=group.id, user_id=None, guest_member_id=member.id,
                       action_id='guest-action', revision=4, kind='patch', changes={}))
    db.commit()
assert next(c for c in inspect(engine).get_columns('group_members') if c['name'] == 'user_id')['nullable']
print('Legacy SQLite group migration preserved members, action history and shared progress; repeat migration and guest writes passed.')
