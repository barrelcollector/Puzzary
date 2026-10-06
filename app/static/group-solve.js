(() => {
  const panel = document.getElementById('group-solve');
  if (!panel) return;
  const id = panel.dataset.group;
  const status = panel.querySelector('#group-status');
  let adapter, snapshot, baseline = {}, busy = false, ready = false, focus = '', lastCompleted = false, notice = '';
  let renderedState = '', renderedRevision = -1, mutation = 0;
  const memberNodes = new Map();
  const board = document.querySelector('.puzzle-layout > .panel');
  async function api(url, body) {
    const response = await fetch(url, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || 'Group request failed');
      error.status = response.status; throw error;
    }
    return data;
  }
  function options(form) { return {participant_limit:Number(form.elements.participant_limit.value), approval_required:form.elements.approval_required.checked}; }
  for (const [selector, url, body] of [
    ['#group-create', `/api/puzzles/${panel.dataset.puzzle}/groups`, options],
    ['#group-join', '/api/groups/join', form => ({code:form.elements.code.value.trim()})]
  ]) panel.querySelector(selector)?.addEventListener('submit', async event => {
    event.preventDefault();
    try { const result = await api(url, body(event.target)); location.assign(result.url); }
    catch (error) { status.textContent = error.message; }
  });
  if (!id) return;
  function setText(element, value) { if (element.textContent !== value) element.textContent = value; }
  function message(value) { setText(panel.querySelector('#group-message'),value); }
  const inviteUrl = new URL(`/puzzles/${panel.dataset.puzzle}`,location.origin);
  inviteUrl.searchParams.set('group',id);
  panel.querySelector('#group-invite-url').value = inviteUrl.href;
  function applyState(state) {
    if (!adapter) return;
    const encoded = JSON.stringify(state);
    const revision = snapshot?.revision ?? -1;
    if (encoded === renderedState && renderedRevision === revision && !Object.keys(diff(adapter.read(),state.answers)).length) return;
    adapter.apply(state); renderedState = encoded; renderedRevision = revision;
  }
  function block(disabled) {
    board.classList.toggle('group-blocked', disabled);
    board.inert = disabled;
    document.getElementById('reveal-solution')?.toggleAttribute('disabled', disabled);
    panel.querySelector('#group-check').disabled = disabled;
    panel.querySelector('#group-hint').disabled = disabled;
  }
  block(true);
  function diff(a, b) {
    return Object.fromEntries([...new Set([...Object.keys(a), ...Object.keys(b)])]
      .filter(k => (a[k] || '') !== (b[k] || '')).map(k => [k, a[k] || '']));
  }
  function show(data, preserve = {}) {
    if (snapshot && data.revision < snapshot.revision) return;
    snapshot = data;
    baseline = {...data.state.answers};
    const merged = {...baseline};
    for (const [k,v] of Object.entries(preserve)) { if (v) merged[k] = v; else delete merged[k]; }
    applyState({...data.state, answers:merged});
    block(data.closed);
    setText(panel.querySelector('#group-code'),data.code);
    let summary = data.closed ? 'Session closed. Shared progress is preserved.' :
      data.completed ? 'Group puzzle complete.' : `${data.found_count} of ${data.expected_count} correct. Shared progress.`;
    if (notice) summary = `${notice} ${summary}`;
    setText(status,summary);
    if (ready && data.completed && !lastCompleted) window.PuzzaryCelebrate?.complete();
    lastCompleted = data.completed;
    const host = data.user_id === data.host_id;
    panel.querySelector('#group-host').hidden = !host;
    const list = panel.querySelector('#group-members');
    const visible = data.members.filter(m => ['active','pending'].includes(m.status));
    for (const [userId,item] of memberNodes) {
      if (!visible.some(m => m.user_id === userId)) { item.remove(); memberNodes.delete(userId); }
    }
    for (const member of visible) {
      let item = memberNodes.get(member.user_id);
      if (!item) {
        item = document.createElement('span'); item.className = 'group-member';
        item.append(document.createElement('span'),document.createElement('span'));
        memberNodes.set(member.user_id,item); list.append(item);
      }
      setText(item.firstElementChild,`${member.name}${member.user_id === data.host_id ? ' (host)' : ''} - ${member.status === 'pending' ? 'awaiting approval' : member.online ? 'online' : 'away'}`);
      const actions = `${host}:${data.closed}:${member.status}`;
      if (item.dataset.actions === actions) continue;
      item.dataset.actions = actions;
      const controls = item.lastElementChild; controls.replaceChildren();
      if (host && member.user_id !== data.host_id && !data.closed) {
        for (const action of member.status === 'pending' ? ['approve','remove'] : ['remove']) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = action === 'approve' ? 'Approve' : 'Remove';
          button.addEventListener('click',async () => {
            mutation++;
            try { show(await api(`/api/groups/${id}/members`,{user_id:member.user_id,action}), adapter ? diff(adapter.read(),baseline) : {}); }
            catch(error) { message(error.message); }
          }); controls.append(button);
        }
      }
    }
    const cells = board.querySelectorAll('[data-key]');
    cells.forEach(cell => {
      const key = cell.dataset.key;
      const contributor = data.recent.find(a => a.keys.includes(key));
      cell.classList.toggle('group-recent',!!contributor && contributor.user_id !== data.user_id && contributor.revision > data.revision-3);
      cell.classList.toggle('group-focus',data.members.some(m => m.user_id !== data.user_id && m.status === 'active' && m.online && m.focus === key));
      const title = contributor ? `Last edit: ${contributor.name}` : '';
      if (cell.title !== title) cell.title = title;
    });
  }
  window.PuzzaryGroup = {
    register(value) { adapter = value; renderedState = ''; if (snapshot) applyState(snapshot.state); },
    save() { send('patch'); }
  };
  async function send(kind, target = '') {
    if (!ready || busy || snapshot?.closed || !adapter) return;
    const before = adapter.read(), changes = diff(before, baseline);
    if (kind !== 'patch' && Object.keys(changes).length) {
      if (!await send('patch')) return false;
      if (!Object.keys(diff(adapter.read(),baseline)).length) return send(kind,target);
      return;
    }
    if (kind === 'patch' && !Object.keys(changes).length) return;
    busy = true;
    mutation++;
    let retryDelay = 100;
    let success = false;
    try {
      const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map(b => b.toString(16).padStart(2,'0')).join('');
      const actionId = `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
      const data = await api(`/api/groups/${id}/actions`, {action_id:actionId,revision:snapshot.revision,kind,target,changes:kind === 'patch' ? changes : {}});
      notice = ''; success = true;
      show(data, diff(adapter.read(), before));
    } catch (error) {
      retryDelay = 2000;
      notice = error.message;
      if (error.status === 409 || error.status === 422) {
        try { show(await api(`/api/groups/${id}`)); } catch (_) {}
      } else if (error.status === 403) { ready = false; block(true); }
      status.textContent = error.message;
    } finally { busy = false; }
    if (ready && Object.keys(diff(adapter.read(),baseline)).length) setTimeout(() => send('patch'),retryDelay);
    return success;
  }
  panel.querySelector('#group-check').addEventListener('click',() => send('check'));
  panel.querySelector('#group-hint').addEventListener('click',() => send('hint'));
  document.addEventListener('click',event => {
    const button = event.target.closest('button'); if (!button) return;
    let kind = button.id === 'reveal-solution' ? 'reveal' :
      board.contains(button) && button.classList.contains('more-clue') ? 'clue' :
      board.contains(button) && button.classList.contains('reveal-entry') ? 'reveal_entry' :
      board.contains(button) && button.classList.contains('check-entry') ? 'check_entry' :
      board.contains(button) && (button.id.endsWith('-check') || button.classList.contains('takuzu-check') || button.classList.contains('nonogram-check')) ? 'check' :
      board.contains(button) && button.classList.contains('takuzu-hint') ? 'hint' : '';
    if (!kind) return;
    event.preventDefault(); event.stopImmediatePropagation(); send(kind,button.dataset.entryNumber ? `${button.dataset.entryNumber}:${button.dataset.entryDirection}` : '');
  },true);
  board.addEventListener('focusin',event => { focus = event.target.dataset.key || ''; });
  board.addEventListener('focusout',() => { focus = ''; });
  panel.querySelector('#group-copy').addEventListener('click',async () => {
    panel.querySelector('#group-invite').open = true;
    try { await navigator.clipboard.writeText(inviteUrl.href); message('Invite link copied.'); }
    catch (_) { message('Clipboard unavailable. The invite link is shown above.'); }
  });
  panel.querySelector('#group-leave').addEventListener('click',async () => {
    mutation++;
    try { await api(`/api/groups/${id}/leave`,{}); location.assign(`/puzzles/${panel.dataset.puzzle}`); }
    catch(error) { message(error.message); }
  });
  panel.querySelector('#group-close').addEventListener('click',async () => {
    if (!confirm('Close this Group Solve for everyone?')) return;
    mutation++;
    try { show(await api(`/api/groups/${id}/close`,{})); }
    catch(error) { message(error.message); }
  });
  panel.querySelector('#group-settings').addEventListener('submit',async event => {
    event.preventDefault();
    mutation++;
    try { show(await api(`/api/groups/${id}/settings`,options(event.target)),diff(adapter.read(),baseline)); message('Session settings saved.'); }
    catch(error) { message(error.message); }
  });
  async function connect() {
    try {
      const joined = await api('/api/groups/join',{code:id});
      if (joined.pending) { status.textContent = 'Waiting for host approval.'; setTimeout(connect,2000); return; }
      show(joined); ready = true;
      const form = panel.querySelector('#group-settings');
      form.elements.participant_limit.value = joined.participant_limit;
      form.elements.approval_required.checked = joined.approval_required;
      poll();
    } catch(error) {
      status.textContent = error.message;
      if (!error.status || error.status >= 500) setTimeout(connect,2000);
    }
  }
  async function poll() {
    if (!ready) return;
    if (!busy) {
      const generation = mutation;
      try {
        const data = await api(`/api/groups/${id}?focus=${encodeURIComponent(focus)}`);
        if (busy || generation !== mutation || data.revision < snapshot.revision) { setTimeout(poll,1000); return; }
        if (adapter && !Object.keys(diff(adapter.read(),baseline)).length) show(data);
        if (data.closed) { show(data); return; }
      } catch(error) {
        if (error.status === 403) { ready = false; block(true); status.textContent = error.message; return; }
        status.textContent = 'Connection interrupted. Reconnecting; unsent edits are kept.';
      }
    }
    setTimeout(poll,1000);
  }
  connect();
})();
