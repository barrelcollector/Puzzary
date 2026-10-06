(() => {
  const root = document.querySelector('.nonogram-play');
  if (!root) return;
  const cells = [...root.querySelectorAll('button.nonogram-cell')];
  const status = root.querySelector('.nonogram-status');
  const solution = JSON.parse(root.dataset.solution);
  const saved = JSON.parse(root.dataset.answers || '{}');
  const autoConfirmRows = root.dataset.autoConfirmRows === 'true';
  const autoConfirmColumns = root.dataset.autoConfirmColumns === 'true';
  const rows = [...root.querySelectorAll('tbody tr')];
  const rowCells = rows.map(row => [...row.querySelectorAll('button.nonogram-cell')]);
  const columns = [...root.querySelectorAll('thead th[scope="col"]')];
  let completed = false;
  let mode = '1', brush = '', protectedValue = null, dragging = false, timer, saving = false, dirty = false;
  function confirmLines(notify = true) {
    let newlyConfirmed = false;
    function confirm(clue, line, expected, enabled, label) {
      const complete = enabled && line.every((cell, i) =>
        (cell.dataset.value || '0') === String(expected[i])) &&
        (expected.some(v => v === 1) || line.every(cell => cell.dataset.value === '0'));
      newlyConfirmed ||= complete && !clue.classList.contains('confirmed');
      clue.classList.toggle('confirmed', complete);
      clue.setAttribute('aria-label', `${label} clues${complete ? ', confirmed' : ''}`);
      clue.title = complete ? `${label} confirmed` : '';
    }
    rows.forEach((row, y) => confirm(row.querySelector('th'), rowCells[y], solution[y], autoConfirmRows, `Row ${y + 1}`));
    columns.forEach((clue, x) => confirm(clue, rowCells.map(row => row[x]), solution.map(row => row[x]), autoConfirmColumns, `Column ${x + 1}`));
    const solved = rowCells.every((row, y) => row.every((cell, x) =>
      (cell.dataset.value || '0') === String(solution[y][x])));
    if (notify && solved && !completed) {
      status.textContent = 'Nonogram complete.';
      window.PuzzaryCelebrate?.complete();
    } else if (notify && newlyConfirmed) window.PuzzarySounds?.progress();
    if (!solved && completed && status.textContent === 'Nonogram complete.') status.textContent = '';
    completed = solved;
  }
  function paint(cell, value, preserveValue = null) {
    if (!cell || cell.disabled) return;
    if (preserveValue !== null && cell.dataset.value === preserveValue) return;
    cell.dataset.value = value;
    cell.textContent = value === '0' ? 'X' : '';
    cell.classList.remove('incorrect');
    cell.setAttribute('aria-label', `${cell.dataset.key}: ${value === '1' ? 'filled' : value === '0' ? 'empty' : 'unmarked'}`);
  }
  function answers() {
    return Object.fromEntries(cells.filter(c => !c.disabled && c.dataset.value !== '').map(c => [c.dataset.key, c.dataset.value]));
  }
  async function save() {
    if (window.PuzzaryGroup) { window.PuzzaryGroup.save(); return; }
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
  function queueSave(notify = true) { confirmLines(window.PuzzaryGroup ? false : notify); clearTimeout(timer); timer = setTimeout(save, 200); }
  window.PuzzaryGroup?.register({read:answers, apply(state) {
    cells.forEach(cell => {
      paint(cell,state.answers[cell.dataset.key] || '');
      cell.classList.toggle('incorrect',(state.incorrect || []).includes(cell.dataset.key));
    }); confirmLines(false);
  }});
  cells.forEach(c => { if (!c.disabled && ['0','1'].includes(saved[c.dataset.key])) paint(c, saved[c.dataset.key]); });
  confirmLines(false);
  if (completed) status.textContent = 'Nonogram complete.';
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
    protectedValue = chosen === '1' ? '0' : chosen === '0' ? '1' : null;
    brush = cell.dataset.value === chosen ? '' : chosen;
    paint(cell, brush, protectedValue);
    root.setPointerCapture(event.pointerId);
    queueSave();
  });
  root.addEventListener('pointermove', event => {
    if (!dragging) return;
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('button.nonogram-cell');
    if (target && root.contains(target)) { paint(target, brush, protectedValue); queueSave(); }
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
      paint(cell, cell.dataset.value === value ? '' : value, value === '1' ? '0' : value === '0' ? '1' : null);
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
    queueSave(false);
  });
})();
