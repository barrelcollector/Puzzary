(() => {
  const panel = document.getElementById('group-solve');
  if (!panel) return;
  const id = panel.dataset.group;
  const status = panel.querySelector('#group-status');
  let adapter, snapshot, baseline = {}, busy = false, ready = false, focus = '', lastCompleted = false, notice = '';
  let renderedState = '', renderedRevision = -1, mutation = 0;
  let pendingState = null, stateGeneration = 0, groupSaveTimer = null, lastPoll = 0;
  let moving = false, navigating = false;
  function followPuzzle(data) {
    if (snapshot && data.revision < snapshot.revision) return false;
    if (data.puzzle_id === panel.dataset.puzzle) return false;
    if (!navigating) {
      navigating = true; ready = false; block(true); presenceSocket?.close();
      status.textContent = 'The host moved the group. Opening the next puzzle...';
      location.replace(data.url);
    }
    return true;
  }
  function receiveState(data) {
    if (followPuzzle(data)) return;
    stateGeneration++;
    if (!pendingState || data.revision >= pendingState.revision) pendingState = data;
    flushState();
  }
  function flushState() {
    if (!pendingState || !adapter || busy || Object.keys(diff(adapter.read(),baseline)).length) return;
    const data = pendingState; pendingState = null; show(data);
  }
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
  function options(form) { return {participant_limit:Number(form.elements.participant_limit.value), approval_required:form.elements.approval_required.checked, allow_guests:!!form.elements.allow_guests?.checked}; }
  for (const [selector, url, body] of [
    ['#group-create', `/api/puzzles/${panel.dataset.puzzle}/groups`, options],
    ['#group-join', '/api/groups/join', form => ({code:form.elements.code.value.trim()})]
  ]) panel.querySelector(selector)?.addEventListener('submit', async event => {
    event.preventDefault();
    try { const result = await api(url, body(event.target)); location.assign(result.url); }
    catch (error) { status.textContent = error.message; }
  });
  if (!id) return;
  const overlay = document.createElement('div');
  overlay.className = 'group-presence-layer'; overlay.setAttribute('aria-hidden','true');
  document.body.append(overlay);
  const cursorNodes = new Map();
  const colors = ['#197667','#b33d56','#256ca6','#7951a8','#986013','#167e8c'];
  let pointer = null, pointerAt = 0, presenceMembers = [], presenceReceived = 0;
  const liveMembers = new Map(), memberReceived = new Map();
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let presenceSocket, reconnectDelay = 1000, lastSent = '', sentAt = 0, frameAt = 0;
  function receivePresence(members, replace = false) {
    if (replace) { liveMembers.clear(); memberReceived.clear(); }
    for (const member of members) { liveMembers.set(member.user_id,member); memberReceived.set(member.user_id,performance.now()); }
    renderPresence([...liveMembers.values()]);
  }
  function renderPresence(members) {
    presenceMembers = members; presenceReceived = performance.now();
    const bounds = board.getBoundingClientRect();
    const cells = new Map([...board.querySelectorAll('[data-key]')].map(cell => [cell.dataset.key,cell]));
    const active = new Set();
    if (!snapshot?.closed && !document.hidden) for (const member of members) {
      const value = member.presence || {};
      if (member.user_id === snapshot?.user_id || member.status !== 'active' || !member.online || !value.updated_at || performance.now()-(memberReceived.get(member.user_id) || 0) > 4000) continue;
      active.add(member.user_id);
      let nodes = cursorNodes.get(member.user_id);
      if (!nodes) {
        const cursor = document.createElement('div'); cursor.className = 'group-player-pointer';
        cursor.dataset.user = member.user_id;
        const label = document.createElement('span'); cursor.append(label);
        const ring = document.createElement('div'); ring.className = 'group-player-focus';
        ring.dataset.user = member.user_id;
        const name = document.createElement('span'); name.className = 'group-focus-name'; ring.append(name);
        const caret = document.createElement('i'); caret.className = 'group-player-caret'; ring.append(caret);
        const entry = document.createElement('div'); entry.className = 'group-player-entry';
        overlay.append(cursor,ring,entry);
        nodes = {cursor,label,ring,name,caret,entry}; cursorNodes.set(member.user_id,nodes);
        for (const node of [cursor,ring,entry]) node.style.setProperty('--player-color',colors[Math.abs(member.user_id) % colors.length]);
      }
      setText(nodes.label,member.name); setText(nodes.name,member.name);
      const point = value.pointer;
      nodes.cursor.hidden = !point;
      if (point) {
        const anchor = cells.get(point.key)?.getBoundingClientRect() || bounds;
        const x = anchor.left + point.x * anchor.width, y = anchor.top + point.y * anchor.height;
        if (!nodes.position || nodes.cursor.dataset.visible !== 'true' || reducedMotion.matches) {
          nodes.position = {x,y}; nodes.cursor.style.transform = `translate(${x}px,${y}px)`;
        }
        nodes.target = {x,y};
        nodes.cursor.dataset.visible = 'true';
        nodes.cursor.classList.toggle('label-left',x > innerWidth-160);
      }
      else nodes.cursor.dataset.visible = 'false';
      const cell = cells.get(value.focus);
      nodes.ring.hidden = !cell;
      if (cell) {
        const rect = cell.getBoundingClientRect();
        Object.assign(nodes.ring.style,{left:`${rect.left}px`,top:`${rect.top}px`,width:`${rect.width}px`,height:`${rect.height}px`});
        const length = Math.max(1,cell.value?.length || 1);
        nodes.caret.hidden = value.caret === null || value.caret === undefined;
        nodes.caret.style.left = `${25 + Math.min(1,value.caret / length) * 50}%`;
        nodes.ring.classList.toggle('has-selection',value.selection_end > value.caret);
      }
      const rects = (value.entry || []).map(key => cells.get(key)?.getBoundingClientRect()).filter(Boolean);
      nodes.entry.hidden = !rects.length;
      if (rects.length) {
        const left = Math.min(...rects.map(r => r.left)), top = Math.min(...rects.map(r => r.top));
        Object.assign(nodes.entry.style,{left:`${left}px`,top:`${top}px`,width:`${Math.max(...rects.map(r => r.right))-left}px`,height:`${Math.max(...rects.map(r => r.bottom))-top}px`});
      }
    }
    for (const [userId,nodes] of cursorNodes) if (!active.has(userId)) {
      nodes.cursor.remove(); nodes.ring.remove(); nodes.entry.remove(); cursorNodes.delete(userId);
    }
  }
  function repositionPresence() {
    const received = presenceReceived;
    if (performance.now()-received < 4000) renderPresence(presenceMembers);
    else renderPresence([]);
    presenceReceived = received;
  }
  window.addEventListener('resize',repositionPresence);
  document.addEventListener('scroll',repositionPresence,true);
  board.addEventListener('pointermove',event => {
    if (event.pointerType === 'touch') return;
    const cell = event.target.closest('[data-key]');
    const rect = (cell || board).getBoundingClientRect();
    pointer = {key:cell?.dataset.key || '',x:Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),y:Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))};
    pointerAt = performance.now();
  });
  board.addEventListener('pointerleave',() => { pointer = null; });
  board.addEventListener('pointerdown',event => {
    if (event.pointerType === 'touch') { pointer = null; focus = event.target.closest('[data-key]')?.dataset.key || ''; }
  });
  window.addEventListener('blur',() => { pointer = null; focus = ''; });
  document.addEventListener('visibilitychange',() => { if (document.hidden) { pointer = null; focus = ''; renderPresence([]); } });
  function presenceBody() {
    const element = document.activeElement;
    if (performance.now()-pointerAt >= 4000) pointer = null;
    const focused = board.contains(element) ? element.closest('[data-key]') : null;
    if (document.hidden || !document.hasFocus()) focus = '';
    else if (focused) focus = focused.dataset.key;
    return document.hidden || !document.hasFocus() ? {} : {
      pointer,
      focus, caret: focused?.selectionStart ?? null, selection_end: focused?.selectionEnd ?? null,
      entry: adapter?.presence?.().entry || []
    };
  }
  function sendPresence(timestamp = performance.now()) {
    if (presenceSocket?.readyState !== WebSocket.OPEN || !ready || snapshot?.closed || timestamp-sentAt < 33) return;
    const encoded = JSON.stringify(presenceBody());
    if (encoded === lastSent && timestamp-sentAt < 1000) return;
    if (presenceSocket.bufferedAmount > 12000) return;
    presenceSocket.send(encoded); sentAt = timestamp; lastSent = encoded;
  }
  function animatePresence(timestamp) {
    const delta = Math.min(50,timestamp-(frameAt || timestamp)); frameAt = timestamp;
    sendPresence(timestamp);
    for (const nodes of cursorNodes.values()) {
      if (nodes.cursor.hidden || !nodes.target) continue;
      const amount = reducedMotion.matches ? 1 : 1-Math.exp(-delta/22);
      nodes.position.x += (nodes.target.x-nodes.position.x)*amount;
      nodes.position.y += (nodes.target.y-nodes.position.y)*amount;
      nodes.cursor.style.transform = `translate(${nodes.position.x}px,${nodes.position.y}px)`;
    }
    requestAnimationFrame(animatePresence);
  }
  function connectPresence() {
    if (!ready || snapshot?.closed) return;
    const url = new URL(`/api/groups/${id}/presence/ws`,location.href);
    url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(url.href); presenceSocket = socket;
    socket.addEventListener('open',() => {
      reconnectDelay = 1000; lastSent = ''; sentAt = 0;
      overlay.dataset.transport = 'websocket'; sendPresence();
    });
    socket.addEventListener('message',event => {
      try {
        const data = JSON.parse(event.data);
        if (['ready','presence'].includes(data.type)) receivePresence(data.members,data.type === 'ready');
        if (data.snapshot && ['ready','state'].includes(data.type)) receiveState(data.snapshot);
      } catch (_) {}
    });
    socket.addEventListener('close',event => {
      if (presenceSocket !== socket) return;
      overlay.dataset.transport = 'polling';
      if (event.code !== 1008 && ready && !snapshot?.closed) {
        setTimeout(connectPresence,reconnectDelay); reconnectDelay = Math.min(15000,reconnectDelay*2);
      }
    });
    socket.addEventListener('error',() => socket.close());
  }
  async function presencePoll() {
    if (!ready || snapshot?.closed) { presenceSocket?.close(); renderPresence([]); return; }
    if (presenceSocket?.readyState === WebSocket.OPEN) {
      sendPresence(); repositionPresence(); setTimeout(presencePoll,1000); return;
    }
    overlay.dataset.transport = 'polling';
    try {
      const data = await api(`/api/groups/${id}/presence`,presenceBody());
      if (presenceSocket?.readyState !== WebSocket.OPEN) receivePresence(data.members,true);
    } catch (error) {
      if ([401,403,409].includes(error.status)) { renderPresence([]); return; }
      if (performance.now()-presenceReceived > 4000) renderPresence([]);
    }
    setTimeout(presencePoll,document.hidden ? 3000 : pointer ? 250 : 1000);
  }
  window.addEventListener('pagehide',() => { ready = false; presenceSocket?.close(); });
  window.addEventListener('pageshow',event => {
    if (event.persisted) { ready = true; connectPresence(); presencePoll(); poll(); }
  });
  function setText(element, value) { if (element.textContent !== value) element.textContent = value; }
  function message(value) { setText(panel.querySelector('#group-message'),value); }
  const inviteUrl = new URL(`/groups/${id}`,location.origin);
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
    if (followPuzzle(data)) return;
    if (snapshot && data.revision < snapshot.revision) return;
    snapshot = data;
    panel.dataset.revision = data.revision;
    baseline = {...data.state.answers};
    const merged = {...baseline};
    for (const [k,v] of Object.entries(preserve)) { if (v) merged[k] = v; else delete merged[k]; }
    applyState({...data.state, answers:merged});
    block(data.closed);
    if (data.closed) renderPresence([]);
    setText(panel.querySelector('#group-code'),data.code);
    let summary = data.closed ? 'Session closed. Shared progress is preserved.' :
      data.completed ? 'Group puzzle complete.' : `${data.found_count} of ${data.expected_count} correct. Shared progress.`;
    if (notice) summary = `${notice} ${summary}`;
    setText(status,summary);
    if (ready && data.completed && !lastCompleted) window.PuzzaryCelebrate?.complete();
    lastCompleted = data.completed;
    const host = data.can_moderate;
    panel.querySelector('#group-host').hidden = !host;
    const guestOption = panel.querySelector('#group-guests-option');
    if (guestOption) guestOption.hidden = !data.can_allow_guests;
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
      setText(item.firstElementChild,`${member.name}${member.user_id === data.host_id ? ' (host)' : member.guest ? ' (guest)' : ''} - ${member.status === 'pending' ? 'awaiting approval' : member.online ? 'online' : 'away'}`);
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
    register(value) { adapter = value; renderedState = ''; if (snapshot) applyState(snapshot.state); flushState(); },
    save() {
      if (groupSaveTimer !== null) return;
      groupSaveTimer = setTimeout(() => { groupSaveTimer = null; send('patch'); },40);
    }
  };
  async function send(kind, target = '') {
    if (!ready || busy || moving || snapshot?.closed || !adapter) return;
    const before = adapter.read(), changes = diff(before, baseline);
    if (kind !== 'patch' && Object.keys(changes).length) {
      if (!await send('patch')) return false;
      if (!Object.keys(diff(adapter.read(),baseline)).length) return send(kind,target);
      return;
    }
    if (kind === 'patch' && !Object.keys(changes).length) { flushState(); return; }
    busy = true;
    mutation++;
    let retryDelay = 40;
    let success = false;
    try {
      const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map(b => b.toString(16).padStart(2,'0')).join('');
      const actionId = `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
      const data = await api(`/api/groups/${id}/actions`, {action_id:actionId,revision:snapshot.revision,puzzle_id:panel.dataset.puzzle,kind,target,changes:kind === 'patch' ? changes : {}});
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
    flushState();
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
    try { await api(`/api/groups/${id}/invite`); }
    catch (error) { message(error.message); return; }
    try { await navigator.clipboard.writeText(inviteUrl.href); message('Invite link copied.'); }
    catch (_) { message('Clipboard unavailable. The invite link is shown above.'); }
  });
  panel.querySelector('#group-leave').addEventListener('click',async () => {
    mutation++;
    try { await api(`/api/groups/${id}/leave`,{}); location.assign(snapshot?.is_guest ? '/login' : `/puzzles/${panel.dataset.puzzle}`); }
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
  let puzzleOffset = 0, puzzleQuery = '', pickerGeneration = 0;
  const picker = panel.querySelector('#group-puzzle-picker');
  const moveForm = panel.querySelector('#group-move');
  async function loadPuzzles() {
    const generation = ++pickerGeneration;
    const resultStatus = panel.querySelector('#group-puzzle-message');
    resultStatus.textContent = 'Loading puzzles...';
    moveForm.querySelector('button').disabled = true;
    try {
      const data = await api(`/api/groups/${id}/puzzles?q=${encodeURIComponent(puzzleQuery)}&offset=${puzzleOffset}`);
      if (generation !== pickerGeneration) return;
      const select = moveForm.elements.puzzle_id; select.replaceChildren();
      for (const puzzle of data.puzzles) {
        const option = document.createElement('option'); option.value = puzzle.id;
        option.textContent = `${puzzle.title} - ${puzzle.puzzle_type} / ${puzzle.difficulty}${puzzle.visited ? ' (saved progress)' : ''}`;
        option.disabled = puzzle.id === panel.dataset.puzzle; select.append(option);
      }
      const first = [...select.options].find(o => !o.disabled);
      select.value = first?.value || '';
      moveForm.querySelector('button').disabled = !first || snapshot?.closed;
      panel.querySelector('#group-puzzles-previous').disabled = puzzleOffset === 0;
      panel.querySelector('#group-puzzles-next').disabled = !data.has_more;
      resultStatus.textContent = data.puzzles.length ? `${puzzleOffset+1}-${puzzleOffset+data.puzzles.length}` : 'No matching puzzles.';
    } catch (error) { resultStatus.textContent = error.message; }
  }
  picker.addEventListener('toggle',() => { if (picker.open) loadPuzzles(); });
  panel.querySelector('#group-puzzle-search').addEventListener('submit',event => {
    event.preventDefault(); puzzleQuery = event.target.elements.q.value.trim(); puzzleOffset = 0; loadPuzzles();
  });
  for (const [selector,step] of [['#group-puzzles-previous',-50],['#group-puzzles-next',50]]) {
    panel.querySelector(selector).addEventListener('click',() => { puzzleOffset += step; loadPuzzles(); });
  }
  moveForm.addEventListener('submit',async event => {
    event.preventDefault();
    if (!ready || busy || moving || snapshot?.closed || !moveForm.elements.puzzle_id.value) return;
    if (adapter && Object.keys(diff(adapter.read(),baseline)).length && !await send('patch')) return;
    if (busy || (adapter && Object.keys(diff(adapter.read(),baseline)).length)) {
      message('Wait for your edits to save before moving the group.'); return;
    }
    moving = true; mutation++; block(true); moveForm.querySelector('button').disabled = true;
    try {
      show(await api(`/api/groups/${id}/puzzle`,{puzzle_id:moveForm.elements.puzzle_id.value,revision:snapshot.revision}));
    } catch (error) { message(error.message); }
    finally { moving = false; if (!navigating) { block(snapshot?.closed); moveForm.querySelector('button').disabled = !!snapshot?.closed; } }
  });
  async function connect() {
    try {
      const joined = await api('/api/groups/join',{code:id});
      if (joined.url && !joined.pending && followPuzzle(joined)) return;
      if (joined.pending && joined.url !== location.pathname+location.search) { location.replace(joined.url); return; }
      if (joined.pending) { status.textContent = 'Waiting for host approval.'; setTimeout(connect,2000); return; }
      show(joined); ready = true;
      connectPresence(); requestAnimationFrame(animatePresence);
      presencePoll();
      const form = panel.querySelector('#group-settings');
      form.elements.participant_limit.value = joined.participant_limit;
      form.elements.approval_required.checked = joined.approval_required;
      if (form.elements.allow_guests) form.elements.allow_guests.checked = joined.allow_guests;
      poll();
    } catch(error) {
      status.textContent = error.message;
      if (!error.status || error.status >= 500) setTimeout(connect,2000);
    }
  }
  async function poll() {
    if (!ready) return;
    if (presenceSocket?.readyState === WebSocket.OPEN && performance.now()-lastPoll < 10000) { setTimeout(poll,1000); return; }
    if (!busy) {
      lastPoll = performance.now();
      const generation = mutation, stateVersion = stateGeneration;
      try {
        const data = await api(`/api/groups/${id}?focus=${encodeURIComponent(focus)}`);
        if (followPuzzle(data)) return;
        if (busy || generation !== mutation || stateVersion !== stateGeneration || data.revision < snapshot.revision) { setTimeout(poll,1000); return; }
        if (adapter && !Object.keys(diff(adapter.read(),baseline)).length) show(data);
        if (data.closed) { show(data); return; }
      } catch(error) {
        if (error.status === 401 || error.status === 403) { ready = false; block(true); status.textContent = error.message; return; }
        status.textContent = 'Connection interrupted. Reconnecting; unsent edits are kept.';
      }
    }
    setTimeout(poll,1000);
  }
  connect();
})();
