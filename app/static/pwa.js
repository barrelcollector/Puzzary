(() => {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;

  const connectivity = document.querySelector("[data-pwa-connectivity]");
  const update = document.querySelector("[data-pwa-update]");
  const refresh = document.querySelector("[data-pwa-refresh]");
  const install = document.querySelector("[data-pwa-install]");
  let installPrompt = null;
  let waitingWorker = null;
  let refreshRequested = false;

  function showConnectivity() {
    const offline = !navigator.onLine;
    document.documentElement.classList.toggle("is-offline", offline);
    if (!connectivity) return;
    connectivity.hidden = !offline;
    connectivity.textContent = offline ? "You are offline. Online-only actions will resume when connected." : "";
  }

  function showUpdate(worker) {
    waitingWorker = worker;
    if (update) update.hidden = false;
  }

  function clearUserData() {
    navigator.serviceWorker.controller?.postMessage({ type: "CLEAR_USER_DATA" });
  }

  showConnectivity();
  window.addEventListener("online", showConnectivity);
  window.addEventListener("offline", showConnectivity);

  const scope = document.body.dataset.userScope || "anonymous";
  const priorScope = localStorage.getItem("puzzary-user-scope");
  if (priorScope && priorScope !== scope) clearUserData();
  localStorage.setItem("puzzary-user-scope", scope);
  document.querySelector('form[action="/logout"]')?.addEventListener("submit", clearUserData);

  navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).then((registration) => {
    if (registration.waiting) showUpdate(registration.waiting);
    registration.addEventListener("updatefound", () => {
      const worker = registration.installing;
      worker?.addEventListener("statechange", () => {
        if (worker.state === "installed" && navigator.serviceWorker.controller) showUpdate(worker);
      });
    });
    window.setInterval(() => registration.update().catch(() => {}), 60 * 60 * 1000);
  }).catch(() => {});

  refresh?.addEventListener("click", () => {
    refreshRequested = true;
    waitingWorker?.postMessage({ type: "SKIP_WAITING" });
  });
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (refreshRequested) window.location.reload();
  });

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    if (install) install.hidden = false;
  });
  install?.addEventListener("click", async () => {
    if (!installPrompt) return;
    install.hidden = true;
    await installPrompt.prompt();
    await installPrompt.userChoice;
    installPrompt = null;
  });
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    if (install) install.hidden = true;
  });
})();
