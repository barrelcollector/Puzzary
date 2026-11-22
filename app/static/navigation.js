(() => {
  const header = document.querySelector('.topbar');
  const toggle = header?.querySelector('[data-navigation-toggle]');
  const menu = header?.querySelector('[data-navigation-menu]');
  if (!header || !toggle || !menu) return;

  const narrow = window.matchMedia('(max-width: 1100px)');
  const close = (restoreFocus = false) => {
    toggle.setAttribute('aria-expanded', 'false');
    if (narrow.matches) menu.hidden = true;
    if (restoreFocus) toggle.focus();
  };
  const open = () => {
    menu.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
  };
  const synchronize = () => {
    header.classList.add('navigation-ready');
    toggle.hidden = !narrow.matches;
    if (narrow.matches) close();
    else { menu.hidden = false; toggle.setAttribute('aria-expanded', 'false'); }
  };

  toggle.addEventListener('click', () => toggle.getAttribute('aria-expanded') === 'true' ? close() : open());
  menu.addEventListener('click', event => {
    if (narrow.matches && event.target.closest('a,button')) close();
  });
  document.addEventListener('click', event => {
    if (narrow.matches && toggle.getAttribute('aria-expanded') === 'true' && !header.contains(event.target)) close();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && narrow.matches && toggle.getAttribute('aria-expanded') === 'true') close(true);
  });
  if (narrow.addEventListener) narrow.addEventListener('change', synchronize);
  else narrow.addListener(synchronize);
  synchronize();
})();
