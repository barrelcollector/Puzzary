(() => {
  const form = document.getElementById('group-lobby-filters');
  const list = document.getElementById('group-lobby-list');
  const status = document.getElementById('group-lobby-status');
  const empty = document.getElementById('group-lobby-empty');
  if (!form || !list) return;
  function element(tag,className,text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function render(groups) {
    list.replaceChildren();
    for (const group of groups) {
      const card = element('article','group-lobby-card'); card.dataset.status = group.status;
      const heading = element('div','group-lobby-card-heading');
      heading.append(element('h2','',group.puzzle_title),element('span','group-lobby-state',group.status_label));
      const description = element('p','',`${group.puzzle_type_label} hosted by `);
      description.append(element('strong','',group.host_name));
      const facts = element('div','group-lobby-facts');
      facts.append(element('span','',`${group.players} / ${group.capacity} players`),
        element('span','',group.password_protected ? 'Guest password required' : 'No guest password'),
        element('span','',group.guest_allowed ? 'Guests allowed' : 'Account required'));
      if (group.puzzle_password_protected) facts.append(element('span','','Puzzle password required'));
      let action;
      if (group.joinable) {
        action = element('a','button-link',document.body.dataset.signedIn === 'true' ? 'Join session' : group.guest_allowed ? 'Join as guest' : 'Sign in to join');
        action.href = group.url;
      } else {
        action = element('button','', 'Session full'); action.type = 'button'; action.disabled = true;
      }
      card.append(heading,description,facts,action); list.append(card);
    }
    empty.hidden = groups.length > 0;
    status.textContent = `${groups.length} public session${groups.length === 1 ? '' : 's'}`;
  }
  async function refresh(updateUrl = false) {
    const query = new URLSearchParams(new FormData(form));
    try {
      const response = await fetch(`/api/groups/lobby?${query}`, {headers:{'Accept':'application/json'},cache:'no-store'});
      if (!response.ok) throw new Error('Lobby refresh failed');
      render((await response.json()).groups);
      if (updateUrl) history.replaceState(null,'',`/groups?${query}`);
    } catch (_) {
      status.textContent = 'Could not refresh the lobby. Existing results are still shown.';
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); refresh(true); });
  setInterval(() => { if (!document.hidden) refresh(); },15000);
})();
