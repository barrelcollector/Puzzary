(() => {
  const context = document.getElementById('puzzle-play-context');
  if (!context || context.dataset.group === 'true') return;
  let adapter = null;
  let baseline = {};
  const message = text => {
    const status = document.querySelector('.puzzle-layout [role="status"], .puzzle-layout .muted[id$="status"]');
    if (status) status.textContent = text;
  };
  window.PuzzarySolo = {
    register(value) { adapter = value; baseline = adapter.read ? adapter.read() : {}; },
    async action(kind, target = '') {
      if (!adapter) return false;
      const current = adapter.read ? adapter.read() : {};
      const changes = Object.fromEntries([...new Set([...Object.keys(baseline),...Object.keys(current)])]
        .filter(key => (baseline[key] || '') !== (current[key] || ''))
        .map(key => [key,current[key] || '']));
      const response = await fetch(`/api/puzzles/${context.dataset.puzzle}/actions`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({kind, target, changes}),
      });
      const state = await response.json();
      if (!response.ok) {
        message(state.detail || 'Puzzle action failed.');
        return false;
      }
      adapter.apply(state);
      baseline = adapter.read ? adapter.read() : {};
      return true;
    },
  };
})();
