(() => {
  const form = document.querySelector('[data-word-bank-picker]');
  if (!form) return;
  const search = form.querySelector('[data-picker-search]');
  const value = form.querySelector('[data-picker-value]');
  const results = form.querySelector('[data-picker-results]');
  const status = form.querySelector('[data-picker-status]');
  const submit = form.querySelector('[data-picker-submit]');
  let timer = 0;
  let request = null;
  let options = [];
  let active = -1;

  function close() {
    results.hidden = true;
    search.setAttribute('aria-expanded', 'false');
    search.removeAttribute('aria-activedescendant');
    active = -1;
  }

  function activate(index) {
    if (!options.length) return;
    active = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((option, position) => option.classList.toggle('is-active', position === active));
    const option = options[active];
    search.setAttribute('aria-activedescendant', option.id);
    option.scrollIntoView({ block: 'nearest' });
  }

  function choose(option) {
    value.value = option.dataset.value;
    search.value = option.dataset.label;
    search.setCustomValidity('');
    submit.disabled = false;
    status.textContent = `Selected ${option.dataset.label}`;
    close();
  }

  function render(items, hasMore) {
    results.replaceChildren();
    for (const [index, item] of items.entries()) {
      const option = document.createElement('button');
      option.type = 'button';
      option.id = `word-bank-entry-option-${index}`;
      option.className = 'word-bank-entry-option';
      option.setAttribute('role', 'option');
      option.dataset.value = item.value;
      option.dataset.label = item.label;
      const word = document.createElement('strong');
      word.textContent = item.word;
      const definition = document.createElement('small');
      definition.textContent = item.definition;
      option.append(word, definition);
      option.addEventListener('pointerdown', event => event.preventDefault());
      option.addEventListener('click', () => choose(option));
      results.append(option);
    }
    options = [...results.querySelectorAll('[role="option"]')];
    active = -1;
    results.hidden = options.length === 0;
    search.setAttribute('aria-expanded', options.length ? 'true' : 'false');
    status.textContent = options.length
      ? `${options.length} option${options.length === 1 ? '' : 's'} found${hasMore ? '; refine your search for more' : ''}.`
      : 'No available words or definitions match that search.';
  }

  async function load() {
    const query = search.value.trim();
    if (query.length < 2) {
      if (request) request.abort();
      results.replaceChildren();
      options = [];
      close();
      status.textContent = 'Type at least 2 characters to search.';
      return;
    }
    request?.abort();
    request = new AbortController();
    status.textContent = 'Searching...';
    try {
      const response = await fetch(`${form.dataset.pickerEndpoint}?q=${encodeURIComponent(query)}`, {
        headers: { Accept: 'application/json' }, signal: request.signal,
      });
      if (!response.ok) throw new Error('Search is temporarily unavailable.');
      const data = await response.json();
      render(data.items || [], Boolean(data.has_more));
    } catch (error) {
      if (error.name === 'AbortError') return;
      results.replaceChildren();
      options = [];
      close();
      status.textContent = error.message;
    }
  }

  search.addEventListener('input', () => {
    value.value = '';
    submit.disabled = true;
    search.setCustomValidity('');
    clearTimeout(timer);
    timer = setTimeout(load, 180);
  });
  search.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!options.length) return;
      event.preventDefault();
      activate(event.key === 'ArrowDown' ? (active + 1) % options.length : (active <= 0 ? options.length - 1 : active - 1));
    } else if (event.key === 'Enter' && active >= 0) {
      event.preventDefault();
      choose(options[active]);
    } else if (event.key === 'Escape') {
      close();
    }
  });
  form.addEventListener('submit', event => {
    if (value.value) return;
    event.preventDefault();
    search.setCustomValidity('Choose a word or definition from the search results.');
    search.reportValidity();
  });
})();
