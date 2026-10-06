(() => {
  const form = document.getElementById('issue-editor');
  if (!form) return;
  const modules = JSON.parse(document.getElementById('issue-initial-modules').textContent);
  const puzzles = new Map(JSON.parse(document.getElementById('issue-puzzle-data').textContent).map(p => [p.id, p]));
  const list = document.getElementById('issue-sections');
  const picker = document.getElementById('issue-puzzle-picker');
  const state = document.getElementById('issue-save-state');
  let dragged = null;
  function dirty() { state.textContent = 'Unsaved changes'; }
  function button(label, title, action, disabled = false) {
    const el = document.createElement('button');
    el.type = 'button'; el.className = 'ghost-button issue-icon-button';
    el.textContent = label; el.title = title; el.setAttribute('aria-label', title);
    el.disabled = disabled; el.addEventListener('click', action); return el;
  }
  function draw() {
    list.replaceChildren();
    modules.forEach((module, index) => {
      const row = document.createElement('div'); row.className = 'issue-module-row';
      row.draggable = module.kind === 'puzzle';
      row.addEventListener('dragstart', event => { dragged = index; event.dataTransfer.setData('text/plain', String(index)); });
      row.addEventListener('dragover', event => { if (dragged !== null) event.preventDefault(); });
      row.addEventListener('drop', event => {
        event.preventDefault(); if (dragged === null) return;
        const [item] = modules.splice(dragged, 1); modules.splice(index, 0, item);
        dragged = null; dirty(); draw();
      });
      row.addEventListener('dragend', () => { dragged = null; });
      const heading = document.createElement('div'); heading.className = 'issue-module-heading';
      const name = document.createElement('strong');
      name.textContent = `${index + 1}. ${module.kind === 'puzzle' ? (puzzles.get(module.puzzle_id)?.title || module.puzzle_id) : 'Text section'}`;
      heading.append(name);
      const controls = document.createElement('div'); controls.className = 'action-row';
      for (const [delta, label, title] of [[-1, '\u2191', 'Move section up'], [1, '\u2193', 'Move section down']]) {
        controls.append(button(label, title, () => {
          [modules[index], modules[index + delta]] = [modules[index + delta], modules[index]];
          dirty(); draw();
        }, index + delta < 0 || index + delta >= modules.length));
      }
      controls.append(button('\u00d7', 'Remove section', () => { modules.splice(index, 1); dirty(); draw(); }));
      heading.append(controls); row.append(heading);
      if (module.kind === 'text') {
        for (const [key, label, max] of [['title', 'Section title', 160], ['body', 'Content', 10000], ['attribution', 'Attribution / source', 500]]) {
          const wrapper = document.createElement('label'); wrapper.textContent = label;
          const field = document.createElement(key === 'body' ? 'textarea' : 'input');
          field.value = module[key] || ''; field.maxLength = max;
          field.required = key !== 'attribution'; if (key === 'body') field.rows = 4;
          field.addEventListener('input', () => { module[key] = field.value; dirty(); });
          wrapper.append(field); row.append(wrapper);
        }
      } else {
        const type = document.createElement('small');
        type.textContent = puzzles.get(module.puzzle_id)?.puzzle_type || 'Source puzzle unavailable'; row.append(type);
      }
      list.append(row);
    });
    document.getElementById('issue-section-count').textContent = `${modules.length} / 20`;
    document.getElementById('issue-section-empty').hidden = modules.length > 0;
    document.getElementById('issue-add-puzzle').disabled = modules.length >= 20;
    document.getElementById('issue-add-text').disabled = modules.length >= 20;
  }
  document.getElementById('issue-puzzle-search').addEventListener('input', event => {
    const query = event.target.value.toLowerCase();
    for (const option of picker.options) {
      option.hidden = !option.textContent.toLowerCase().includes(query);
      if (option.hidden) option.selected = false;
    }
  });
  document.getElementById('issue-add-puzzle').addEventListener('click', () => {
    for (const option of picker.selectedOptions) {
      if (modules.length >= 20) break;
      modules.push({kind: 'puzzle', puzzle_id: option.value});
    }
    dirty(); draw();
  });
  document.getElementById('issue-add-text').addEventListener('click', () => {
    if (modules.length >= 20) return;
    modules.push({kind: 'text', title: '', body: '', attribution: ''}); dirty(); draw();
  });
  form.addEventListener('input', dirty);
  form.addEventListener('submit', () => { document.getElementById('issue-modules-json').value = JSON.stringify(modules); });
  draw();
})();
