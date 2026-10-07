(() => {
  const form = document.querySelector('[data-permission-form]');
  const update = () => {
    if (!form) return;
    const inherited = new Set();
    form.querySelectorAll('[data-grants]:checked').forEach(input => JSON.parse(input.dataset.grants).forEach(p => inherited.add(p)));
    let count = 0;
    form.querySelectorAll('[data-permission]').forEach(input => {
      const allowed = form.dataset.active === 'true' && (input.value === 'allow' || (input.value === 'inherit' && inherited.has(input.dataset.permission)));
      input.closest('.permission-row').querySelector('[data-effective]').textContent = allowed ? 'Allowed' : 'Denied';
      if (allowed) count++;
    });
    form.querySelector('[data-preview-count]').textContent = `${count} effective permissions`;
  };
  if (form) {
    form.addEventListener('change', update);
    form.querySelectorAll('[data-bulk]').forEach(button => button.addEventListener('click', () => {
      button.closest('details').querySelectorAll('select').forEach(input => input.value = button.dataset.bulk);
      update();
    }));
    form.addEventListener('submit', event => {
      if (!window.confirm('Apply these access changes? High-privilege grants can allow account, role or server administration.')) event.preventDefault();
    });
    update();
  }
  document.querySelectorAll('[data-duplicate]').forEach(button => button.addEventListener('click', event => {
    const name = window.prompt('Name for the duplicate role');
    if (!name || !name.trim()) { event.preventDefault(); return; }
    button.form.elements.name.value = name.trim();
  }));
  document.querySelectorAll('[data-role-form]').forEach(roleForm => roleForm.addEventListener('submit', event => {
    if (!window.confirm('Save this role? Access changes apply to every assigned account.')) event.preventDefault();
  }));
})();
