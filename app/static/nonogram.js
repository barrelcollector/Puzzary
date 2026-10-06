(() => {
  const root = document.querySelector('.nonogram-play');
  if (!root) return;
  const cells = [...root.querySelectorAll('button.nonogram-cell')];
  const status = root.querySelector('.nonogram-status');
  const solution = JSON.parse(root.dataset.solution);
  const saved = JSON.parse(root.dataset.answers || '{}');
  let mode = '1', brush = '', dragging = false, timer, saving = false, dirty = false;
  function paint(cell, value) {
    if (!cell || cell.disabled) return;
    cell.dataset.value = value;
    cell.textContent = value === '0' ? 'X' : '';
    cell.classList.remove('incorrect');
    cell.setAttribute('aria-label', `${cell.dataset.key}: ${value === '1' ? 'filled' : value === '0' ? 'empty' : 'unmarked'}`);
  }
  function answers() {
    return Object.fromEntries(cells.filter(c => !c.disabled && c.dataset.value !== '').map(c => [c.dataset.key, c.dataset.value]));
  }
  async function save() {
    dirty = true;
    if (saving) return;
    saving = true;
    while (dirty) {
      dirty = false;
      try {
        const response = await fetch(root.dataset.progressUrl, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({answers:answers()})});
        if (!response.ok) throw new Error('Save failed');
      } catch (_) {
        status.textContent = 'Progress could not be saved. Make another mark to retry.';
        break;
      }
    }
    saving = false;
  }
  function queueSave() { clearTimeout(timer); timer = setTimeout(save, 200); }
  cells.forEach(c => { if (!c.disabled && ['0','1'].includes(saved[c.dataset.key])) paint(c, saved[c.dataset.key]); });
  root.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {
    mode = button.dataset.mode;
    root.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
  }));
  root.addEventListener('pointerdown', event => {
    const cell = event.target.closest('button.nonogram-cell');
    if (!cell || cell.disabled || event.button > 2 || event.button === 1) return;
    event.preventDefault();
    cell.focus();
    dragging = true;
    const chosen = event.button === 2 ? '0' : mode;
    brush = cell.dataset.value === chosen ? '' : chosen;
    paint(cell, brush);
    root.setPointerCapture(event.pointerId);
    queueSave();
  });
  root.addEventListener('pointermove', event => {
    if (!dragging) return;
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('button.nonogram-cell');
    if (target && root.contains(target)) { paint(target, brush); queueSave(); }
  });
  function finish() { if (dragging) { dragging = false; queueSave(); } }
  root.addEventListener('pointerup', finish);
  root.addEventListener('pointercancel', finish);
  root.addEventListener('lostpointercapture', finish);
  root.addEventListener('contextmenu', event => { if (event.target.closest('.nonogram-cell')) event.preventDefault(); });
  root.addEventListener('keydown', event => {
    const cell = event.target.closest('button.nonogram-cell');
    if (!cell) return;
    const [x,y] = cell.dataset.key.split(',').map(Number);
    const moves = {ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
    if (moves[event.key]) {
      event.preventDefault();
      const [dx,dy] = moves[event.key];
      cells.find(c => c.dataset.key === `${x+dx},${y+dy}`)?.focus();
    } else if ([' ', 'Enter', 'x', 'X', 'Delete', 'Backspace'].includes(event.key)) {
      event.preventDefault();
      const value = /x/i.test(event.key) ? '0' : ['Delete','Backspace'].includes(event.key) ? '' : mode;
      paint(cell, cell.dataset.value === value ? '' : value);
      queueSave();
    }
  });
  root.querySelector('.nonogram-check').addEventListener('click', () => {
    let wrong = 0, missing = 0;
    for (const cell of cells) {
      const [x,y] = cell.dataset.key.split(',').map(Number);
      const expected = String(solution[y][x]);
      const incorrect = cell.dataset.value !== '' && cell.dataset.value !== expected;
      cell.classList.toggle('incorrect', incorrect);
      if (incorrect) wrong++;
      if (expected === '1' && cell.dataset.value === '') missing++;
    }
    status.textContent = wrong || missing ? `${wrong} incorrect marks. ${missing} filled cells still missing.` : 'Nonogram complete.';
    if (!wrong && !missing) window.PuzzaryCelebrate?.complete();
    queueSave();
  });
  root.querySelector('.nonogram-clear').addEventListener('click', () => {
    cells.forEach(c => paint(c, ''));
    status.textContent = 'Grid cleared.';
    queueSave();
  });
  document.getElementById('reveal-solution')?.addEventListener('click', () => {
    for (const cell of cells) {
      const [x,y] = cell.dataset.key.split(',').map(Number);
      paint(cell, String(solution[y][x]));
    }
    status.textContent = 'Solution revealed.';
    queueSave();
  });
})();
