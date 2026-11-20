"""Host puzzle switching, stale-edit fencing and multi-browser guest following."""
from pathlib import Path
import tempfile
import uuid
from app.database import SessionLocal
from app.models import GroupMember


def verify_moves(browser, base, host, player, first='group-qa', second='qa-sudoku'):
    response = host.request.post(base+f'/api/puzzles/{first}/groups', data={'allow_guests': True})
    assert response.ok, response.text()
    info = response.json(); endpoint = base+'/api/groups/'+info['id']
    assert player.request.post(base+'/api/groups/join', data={'code': info['code']}).ok
    guest = browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    assert guest.request.post(endpoint+'/guests', data={'name':'Move QA guest'}).ok
    guest_id = guest.request.get(endpoint).json()['user_id']
    assert guest.request.post(endpoint+'/puzzle', data={'puzzle_id':second,'revision':0}).status == 401
    assert player.request.post(endpoint+'/puzzle', data={'puzzle_id':second,'revision':0}).status == 403
    assert player.request.get(endpoint+'/puzzles').status == 403
    assert guest.request.get(endpoint+'/puzzles').status == 401
    assert host.request.post(endpoint+'/puzzle', data={'puzzle_id':'missing','revision':0}).status == 404
    assert host.request.post(endpoint+'/puzzle', data={'puzzle_id':second,'revision':100}).status == 409
    listing = host.request.get(endpoint+'/puzzles?q=QA').json()
    assert any(p['id']==second for p in listing['puzzles'])
    # Save a mark, then verify it survives a round trip to another plugin.
    saved = host.request.post(endpoint+'/actions', data={'action_id':str(uuid.uuid4()),'revision':0,
                                                       'changes':{'0,0':'0'}}).json()
    old_revision = saved['revision']
    pages = [context.new_page() for context in [host,player,guest]]
    errors = []
    for page in pages:
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(base+info['url'])
        page.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
        page.wait_for_function('document.querySelector(".group-presence-layer").dataset.transport==="websocket"')
    # The player follows a move even with dirty local work and blocked GET polling.
    pages[1].route('**/api/groups/*',lambda route:route.abort() if route.request.method=='GET' else route.continue_())
    pages[1].evaluate("""() => {const cell=document.querySelector('.takuzu-play [data-key="0,1"]');
      cell.dataset.value='1'; cell.textContent='1';}""")
    hp = pages[0]
    hp.locator('#group-host > summary').click()
    hp.locator('#group-puzzle-picker summary').click()
    hp.wait_for_function('(id)=>!!document.querySelector(`#group-move option[value="${id}"]`)',arg=second)
    hp.locator('#group-move select').select_option(second)
    hp.locator('#group-move button').click()
    for page in pages:
        page.wait_for_url(base+f'/puzzles/{second}?group='+info['id'])
        page.wait_for_function("!document.querySelector('.puzzle-layout > .panel').inert")
    moved = host.request.get(endpoint).json()
    assert moved['state'].get('answers',{}) == {} and moved['id']==info['id'] and moved['code']==info['code']
    assert moved['allow_guests'] and guest.request.get(endpoint).json()['user_id']==guest_id
    assert host.request.get(base+info['invite_url'],max_redirects=0).headers['location']==moved['url']
    assert host.request.get(base+info['url'],max_redirects=0).status==303
    with SessionLocal() as db:
        assert db.query(GroupMember).filter_by(group_id=info['id']).count()==3
    # Old-board edits cannot land even on a coordinate untouched in the new board.
    assert player.request.post(endpoint+'/actions',data={'action_id':str(uuid.uuid4()),'revision':old_revision,
                                                        'changes':{'0,1':'1'}}).status==409
    for claimed_puzzle in [None, first]:
        assert player.request.post(endpoint+'/actions',data={'action_id':str(uuid.uuid4()),'revision':moved['revision'],
                                                            'puzzle_id':claimed_puzzle,'changes':{'0,1':'1'}}).status==409
    before_return = host.request.get(endpoint).json()
    returned = host.request.post(endpoint+'/puzzle',data={'puzzle_id':first,'revision':before_return['revision']})
    assert returned.ok, returned.text()
    assert returned.json()['state']['answers']==saved['state']['answers']
    assert returned.json()['revision']>moved['revision']
    for page in pages:
        page.wait_for_url(base+info['url'])
        page.wait_for_function("document.querySelector('.takuzu-play [data-key=\"0,0\"]').dataset.value==='0'")
    pages[2].locator('.takuzu-play [data-key="0,0"]').press('1')
    hp.wait_for_function("document.querySelector('.takuzu-play [data-key=\"0,0\"]').dataset.value==='1'")
    returned_revision = host.request.get(endpoint).json()['revision']
    # Even revisiting the original puzzle must invalidate its old action revision.
    assert player.request.post(endpoint+'/actions',data={'action_id':str(uuid.uuid4()),'revision':old_revision,
                                                        'changes':{'0,1':'1'}}).status==409
    hp.locator('#group-host > summary').click(); hp.locator('#group-puzzle-picker summary').click()
    hp.wait_for_function('document.querySelector("#group-move select").options.length>0')
    hp.screenshot(path=str(Path(tempfile.gettempdir()) / 'group-move-host.png'),full_page=True)
    assert host.request.post(endpoint+'/close',data={}).ok
    assert host.request.post(endpoint+'/puzzle',data={'puzzle_id':second,'revision':returned_revision}).status==409
    assert not errors, errors
    for page in pages: page.close()
    guest.close()
    print('Host picker, automatic account/guest navigation, stable invites, saved puzzle progress, dirty-client following and stale-edit fencing passed.')
