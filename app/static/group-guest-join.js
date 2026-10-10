(() => {
  const form = document.querySelector('#guest-join');
  if (!form) return;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const response = await fetch(`/api/groups/${form.dataset.group}/guests`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: form.elements.name.value.trim(),password:form.elements.password?.value || ''})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not join this group.');
      location.assign(data.url);
    } catch (error) {
      document.querySelector('#guest-message').textContent = error.message;
      button.disabled = false;
    }
  });
})();
