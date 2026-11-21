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
      if (!navigator.onLine && kind !== 'patch') {
        if (kind === 'check' && window.PuzzaryOfflineCheck) {
          const result = await window.PuzzaryOfflineCheck({answers: current});
          message(result.message);
          if (result.correct) window.PuzzaryCelebrate?.complete();
          return result.correct;
        }
        message(kind === 'check_entry' ? 'Offline checking is available for the completed puzzle, not individual entries.' : 'This action needs a connection. Your offline progress is still saved.');
        return false;
      }
      const changes = Object.fromEntries([...new Set([...Object.keys(baseline),...Object.keys(current)])]
        .filter(key => (baseline[key] || '') !== (current[key] || ''))
        .map(key => [key,current[key] || '']));
      let response;
      try {
        response = await fetch(`/api/puzzles/${context.dataset.puzzle}/actions`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({kind, target, changes}),
        });
      } catch (_error) {
        message('This action needs a connection. Your offline progress is still saved.');
        return false;
      }
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
  const hints = document.querySelector('[data-puzzle-hints]');
  if (hints && hints.querySelector('li')) {
    let recorded = false;
    hints.addEventListener('toggle', () => {
      if (!hints.open || recorded) return;
      recorded = true;
      fetch(`/api/puzzles/${context.dataset.puzzle}/activity`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({kind: 'hint', target: 'general'}),
      }).catch(() => { recorded = false; });
    });
  }
})();
