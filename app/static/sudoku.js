(function () {
  const root = document.querySelector('.sudoku-play');
  if (!root) return;
  const inputs = [...root.querySelectorAll('.sudoku-cell input[data-key]')];
  const status = document.getElementById('sudoku-status');
  const progressUrl = root.dataset.progressUrl;
  const size = Number(root.dataset.size || 9);
  const boxRows = Number(root.dataset.boxRows || 3), boxCols = Number(root.dataset.boxCols || 3);
  const remote = window.PuzzaryGroup || window.PuzzarySolo;
  let saveTimer = null;
  const units = [];
  for (let n = 0; n < size; n++) {
    units.push(inputs.filter(input => Number(input.dataset.key.split(',')[1]) === n));
    units.push(inputs.filter(input => Number(input.dataset.key.split(',')[0]) === n));
  }
  for (let y = 0; y < size; y += boxRows) for (let x = 0; x < size; x += boxCols) {
    units.push(inputs.filter(input => { const [cx,cy] = input.dataset.key.split(',').map(Number); return cx >= x && cx < x+boxCols && cy >= y && cy < y+boxRows; }));
  }
  function answers() { return Object.fromEntries(inputs.filter(i => !i.disabled && i.value.trim()).map(i => [i.dataset.key,i.value.trim()])); }
  function conflicts() {
    const bad = new Set();
    units.forEach(unit => {
      const seen = new Map();
      unit.forEach(input => { const value=input.value.trim(); if (!value) return; if (seen.has(value)) { bad.add(input.dataset.key); bad.add(seen.get(value)); } else seen.set(value,input.dataset.key); });
    });
    return bad;
  }
  function save() {
    if (window.PuzzaryGroup) { window.PuzzaryGroup.save(); return; }
    fetch(progressUrl,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:answers()})}).catch(() => { status.textContent='Progress could not be saved.'; });
  }
  function queueSave() { if (window.PuzzaryGroup) return window.PuzzaryGroup.save(); clearTimeout(saveTimer); saveTimer=setTimeout(save,250); }
  function apply(state) {
    inputs.forEach(input => {
      if (!input.disabled) input.value = state.answers?.[input.dataset.key] || '';
      input.closest('.sudoku-cell').classList.remove('correct','incorrect');
      if (state.revealed) input.closest('.sudoku-cell').classList.add('correct');
    });
    const bad=conflicts(); inputs.forEach(input => input.closest('.sudoku-cell').classList.toggle('incorrect',bad.has(input.dataset.key)));
    if (state.revealed) status.textContent='Solution revealed. Reset the puzzle to start over.';
    else if (state.completed) { status.textContent='Sudoku complete.'; window.PuzzaryCelebrate?.complete(); }
    else if (state.checked) status.textContent=bad.size ? 'Resolve the highlighted row, column, or box conflicts.' : 'No rule conflicts found. Complete every cell to finish.';
  }
  remote?.register({read:answers,apply});
  function at(x,y) { return inputs.find(input => input.dataset.key === `${x},${y}`); }
  function move(input,dx,dy) { let [x,y]=input.dataset.key.split(',').map(Number); do { x+=dx;y+=dy; const next=at(x,y); if(next&&!next.disabled){next.focus();next.select();return;} } while(x>=0&&x<size&&y>=0&&y<size); }
  function set(input,value) { if(input.disabled)return; input.value=String(value).replace(new RegExp(`[^1-${size}]`,'g'),'').slice(0,1); input.closest('.sudoku-cell').classList.remove('correct','incorrect'); queueSave(); }
  inputs.forEach(input => {
    input.addEventListener('input',()=>{set(input,input.value);if(input.value)move(input,1,0);});
    input.addEventListener('keydown',event=>{ if(event.altKey||event.ctrlKey||event.metaKey)return; if(new RegExp(`^[1-${size}]$`).test(event.key)){event.preventDefault();set(input,event.key);move(input,1,0);} else if(event.key==='Backspace'){event.preventDefault();if(input.value)set(input,'');else move(input,-1,0);} else if(event.key==='Delete'||event.key==='0'){event.preventDefault();set(input,'');} else if(event.key==='ArrowRight'){event.preventDefault();move(input,1,0);} else if(event.key==='ArrowLeft'){event.preventDefault();move(input,-1,0);} else if(event.key==='ArrowDown'){event.preventDefault();move(input,0,1);} else if(event.key==='ArrowUp'){event.preventDefault();move(input,0,-1);} });
  });
  document.getElementById('sudoku-check')?.addEventListener('click',()=>root.dataset.checkMode==='none' ? status.textContent='Checking is off for this puzzle.' : remote?.action('check'));
  document.getElementById('sudoku-clear')?.addEventListener('click',()=>{inputs.forEach(i=>{if(!i.disabled)i.value='';i.closest('.sudoku-cell').classList.remove('correct','incorrect');});status.textContent='Grid cleared.';save();});
  document.getElementById('reveal-solution')?.addEventListener('click',()=>remote?.action('reveal'));
})();
