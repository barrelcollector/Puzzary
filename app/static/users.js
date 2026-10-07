(() => {
  const dialog = document.querySelector('[data-user-dialog]');
  document.querySelector('[data-create-user]')?.addEventListener('click', () => dialog.showModal());
  document.querySelector('[data-close-user]')?.addEventListener('click', () => dialog.close());
  const search = document.querySelector('[data-user-search]');
  const status = document.querySelector('[data-user-status]');
  const filter = () => {
    let count = 0;
    document.querySelectorAll('[data-user-card]').forEach(card => {
      card.hidden = !card.dataset.name.includes(search.value.trim().toLowerCase()) || (status.value !== 'all' && card.dataset.status !== status.value);
      if (!card.hidden) count++;
    });
    document.querySelector('[data-user-count]').textContent = `${count} ${count === 1 ? 'user' : 'users'}`;
    document.querySelector('[data-user-empty]').hidden = count !== 0;
  };
  if (search && status) {
    search.addEventListener('input', filter);
    status.addEventListener('change', filter);
    filter();
  }
  const tabs = [...document.querySelectorAll('[data-user-tab]')];
  const activate = (tab, focus = false) => {
    tabs.forEach(button => {
      const selected = button === tab;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    document.querySelectorAll('[data-user-panel]').forEach(panel => panel.hidden = panel.dataset.userPanel !== tab.dataset.userTab);
    const url = new URL(location.href);
    url.searchParams.set('tab', tab.dataset.userTab);
    history.replaceState(null, '', url);
    if (focus) tab.focus();
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activate(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); activate(tabs[next], true); }
    });
  });
  const permissionSearch = document.querySelector('[data-permission-search]');
  permissionSearch?.addEventListener('input', () => {
    const query = permissionSearch.value.trim().toLowerCase();
    document.querySelectorAll('[data-permission-form] details').forEach(group => {
      let matches = 0;
      group.querySelectorAll('.permission-row').forEach(row => {
        row.hidden = !row.querySelector('label').textContent.toLowerCase().includes(query);
        if (!row.hidden) matches++;
      });
      group.hidden = matches === 0;
      if (query && matches) group.open = true;
    });
  });
  document.querySelectorAll('.user-menu').forEach(menu => menu.addEventListener('toggle', () => {
    if (menu.open) document.querySelectorAll('.user-menu').forEach(other => { if (other !== menu) other.open = false; });
  }));
  document.addEventListener('click', event => document.querySelectorAll('.user-menu[open]').forEach(menu => { if (!menu.contains(event.target)) menu.open = false; }));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') document.querySelectorAll('.user-menu[open]').forEach(menu => menu.open = false); });
  document.querySelectorAll('[data-user-panel="security"] form').forEach(form => form.addEventListener('submit', event => {
    if (!confirm(`${form.querySelector('button').textContent.trim()} for this account?`)) event.preventDefault();
  }));
})();
