(() => {
  const root = document.querySelector('.takuzu-play');
  if (!root) return;
  const cells = [...root.querySelectorAll('button.takuzu-cell')];
  const solution = JSON.parse(root.dataset.solution);
  const saved = JSON.parse(root.dataset.answers || '{}');
  const status = root.querySelector('.takuzu-status');
  const autoConfirm = root.dataset.autoConfirm === 'true';
  const rows = [...root.querySelectorAll('tbody tr')];
  const rowCells = rows.map(row => [...row.querySelectorAll('button.takuzu-cell')]);
  const columns = [...root.querySelectorAll('thead th[scope="col"]')];
  let mode = 'cycle', dragging = false, brush = '', timer, saving = false, dirty = false, completed = false;
  function expected(cell) { const [x,y] = cell.dataset.key.split(',').map(Number); return String(solution[y][x]); }
  function correctLine(line) { return line.every(cell => cell.dataset.value === expected(cell)); }
  function solved() { return cells.every(cell => cell.dataset.value === expected(cell)); }
  function paint(cell,value) {
    if (!cell || cell.disabled) return;
    cell.dataset.value = value; cell.textContent = value;
    cell.classList.remove('incorrect');
    const [x,y] = cell.dataset.key.split(',').map(Number);
    cell.setAttribute('aria-label', `Row ${y+1}, column ${x+1}: ${value || 'blank'}`);
  }
  cells.forEach(cell => { if (['0','1'].includes(saved[cell.dataset.key])) paint(cell,saved[cell.dataset.key]); });
  const progressSounds = window.PuzzarySounds?.createProgressTracker(
    () => [...rows.flatMap((row,y) => correctLine(rowCells[y]) ? [`row-${y}`] : []),
      ...columns.flatMap((clue,x) => correctLine(rowCells.map(row => row[x])) ? [`col-${x}`] : [])], solved);
  function confirmLines(enabled) {
    rows.forEach((row,y) => row.querySelector('th').classList.toggle('confirmed',enabled && correctLine(rowCells[y])));
    columns.forEach((clue,x) => clue.classList.toggle('confirmed',enabled && correctLine(rowCells.map(row => row[x]))));
  }
  function update(notify = true) {
    confirmLines(autoConfirm);
    if (notify && autoConfirm) progressSounds?.check();
    else if (notify) progressSounds?.invalidate();
    else progressSounds?.sync();
    const complete = solved();
    if (notify && complete && !completed) {
      status.textContent = 'Takuzu complete.';
      window.PuzzaryCelebrate?.complete();
    } else if (!complete && completed && status.textContent === 'Takuzu complete.') status.textContent = '';
    completed = complete;
  }
  update(false);
  if (completed) status.textContent = 'Takuzu complete.';
  function answers() { return Object.fromEntries(cells.filter(c => !c.disabled && c.dataset.value !== '').map(c => [c.dataset.key,c.dataset.value])); }
  async function save() {
    dirty = true;
    if (saving) return;
    saving = true;
    while (dirty) {
      dirty = false;
      try {
        const response = await fetch(root.dataset.progressUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:answers()})});
        if (!response.ok) throw new Error('Save failed');
      } catch (_) { status.textContent = 'Progress could not be saved. Make another mark to retry.'; break; }
    }
    saving = false;
  }
  function queueSave(notify = true) { update(notify); clearTimeout(timer); timer = setTimeout(save,200); }
  root.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click',() => {
    mode = button.dataset.mode;
    root.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed',String(b === button)));
  }));
  root.addEventListener('pointerdown',event => {
    const cell = event.target.closest('button.takuzu-cell');
    if (!cell || cell.disabled || event.button !== 0) return;
    event.preventDefault(); cell.focus(); dragging = true;
    brush = mode === 'cycle' ? (cell.dataset.value === '' ? '0' : cell.dataset.value === '0' ? '1' : '') : mode;
    paint(cell,brush); root.setPointerCapture(event.pointerId); queueSave();
  });
  root.addEventListener('pointermove',event => {
    if (!dragging) return;
    const cell = document.elementFromPoint(event.clientX,event.clientY)?.closest('button.takuzu-cell');
    if (cell && root.contains(cell)) { paint(cell,brush); queueSave(); }
  });
  function finish() { if (dragging) { dragging = false; queueSave(); } }
  ['pointerup','pointercancel','lostpointercapture'].forEach(name => root.addEventListener(name,finish));
  root.addEventListener('keydown',event => {
    const cell = event.target.closest('button.takuzu-cell');
    if (!cell || event.altKey || event.ctrlKey || event.metaKey) return;
    const [x,y] = cell.dataset.key.split(',').map(Number);
    const moves = {ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
    if (moves[event.key]) {
      event.preventDefault(); const [dx,dy] = moves[event.key];
      cells.find(c => c.dataset.key === `${x+dx},${y+dy}`)?.focus();
    } else if (['0','1',' ','Enter','Delete','Backspace'].includes(event.key)) {
      event.preventDefault();
      const value = ['0','1'].includes(event.key) ? event.key : ['Delete','Backspace'].includes(event.key) ? '' :
        mode === 'cycle' ? (cell.dataset.value === '' ? '0' : cell.dataset.value === '0' ? '1' : '') : mode;
      paint(cell,value); queueSave();
    }
  });
  root.querySelector('.takuzu-check').addEventListener('click',() => {
    let wrong = 0, missing = 0;
    cells.forEach(cell => {
      const incorrect = cell.dataset.value !== '' && cell.dataset.value !== expected(cell);
      cell.classList.toggle('incorrect',incorrect);
      if (incorrect) wrong++;
      if (cell.dataset.value === '') missing++;
    });
    confirmLines(true); progressSounds?.check();
    status.textContent = wrong || missing ? `${wrong} incorrect cells. ${missing} cells still empty.` : 'Takuzu complete.';
    if (!wrong && !missing) window.PuzzaryCelebrate?.complete();
    clearTimeout(timer); timer = setTimeout(save,200);
  });
  root.querySelector('.takuzu-clear').addEventListener('click',() => {
    cells.forEach(c => paint(c,'')); status.textContent = 'Grid cleared.'; queueSave(false);
  });
  root.querySelector('.takuzu-hint').addEventListener('click',() => {
    const cell = cells.find(c => !c.disabled && c.dataset.value !== expected(c));
    if (!cell) return;
    paint(cell,expected(cell));
    const [x,y] = cell.dataset.key.split(',').map(Number);
    status.textContent = `Row ${y+1}, column ${x+1} is ${expected(cell)}.`;
    queueSave(false);
  });
  document.getElementById('reveal-solution')?.addEventListener('click',() => {
    cells.forEach(c => paint(c,expected(c))); status.textContent = 'Solution revealed.'; queueSave(false);
  });
})();
