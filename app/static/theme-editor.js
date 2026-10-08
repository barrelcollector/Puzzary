(() => {
  const form = document.querySelector('[data-theme-editor]');
  if (!form) return;
  const colorNames = ['bg','panel','ink','muted','line','accent','accent_text','focus','success','success_bg','danger','danger_bg','surface_muted','puzzle_cell','puzzle_grid','player_1','player_2','player_3','player_4','player_5','player_6'];
  const defaults = {bg:'#f5f5f2',panel:'#ffffff',ink:'#151515',muted:'#62625c',line:'#d7d5cc',accent:'#1f6f62',accent_text:'#ffffff',focus:'#1268a5',success:'#175f50',success_bg:'#e3f2ec',danger:'#7c2727',danger_bg:'#f8e7e4',surface_muted:'#fbfbf8',puzzle_cell:'#ffffff',puzzle_grid:'#151515',player_1:'#197667',player_2:'#b33d56',player_3:'#256ca6',player_4:'#7951a8',player_5:'#986013',player_6:'#167e8c'};
  const labels = Object.fromEntries(colorNames.map(name => [name,name.replaceAll('_',' ').replace(/^./,letter => letter.toUpperCase())]));
  const colors = form.querySelector('[data-theme-colors]');
  for (const name of colorNames) {
    const label = document.createElement('label'); label.textContent = labels[name];
    const input = document.createElement('input'); input.type = 'color'; input.value = defaults[name]; input.dataset.themeToken = name;
    label.append(input); colors.append(label);
  }
  let existing = {};
  try { existing = JSON.parse(form.dataset.existing || '{}'); } catch (_) {}
  for (const input of form.querySelectorAll('[data-theme-field]')) if (existing[input.dataset.themeField] !== undefined) input.value = existing[input.dataset.themeField];
  for (const input of form.querySelectorAll('[data-theme-token]')) if (existing.tokens?.[input.dataset.themeToken] !== undefined) input.value = existing.tokens[input.dataset.themeToken];
  const preview = form.querySelector('[data-theme-preview]');
  function manifest() {
    const fields = Object.fromEntries([...form.querySelectorAll('[data-theme-field]')].map(input => [input.dataset.themeField,input.value.trim()]));
    const tokens = Object.fromEntries([...form.querySelectorAll('[data-theme-token]')].map(input => [input.dataset.themeToken,input.type === 'number' ? Number(input.value) : input.value]));
    return {schema:'puzzary-theme/v1',...fields,tokens};
  }
  function update() {
    const data = manifest();
    for (const [name,value] of Object.entries(data.tokens)) {
      if (colorNames.includes(name)) preview.style.setProperty(`--${name.replaceAll('_','-')}`,value);
    }
    preview.style.setProperty('--radius',`${data.tokens.radius}px`);
    preview.style.setProperty('--density',data.tokens.density);
    preview.style.setProperty('--font',data.tokens.font === 'serif' ? 'Georgia, serif' : data.tokens.font === 'mono' ? 'ui-monospace, monospace' : 'Inter, system-ui, sans-serif');
    form.elements.manifest_json.value = JSON.stringify(data);
  }
  form.addEventListener('input',update);
  form.addEventListener('submit',update);
  update();
})();
