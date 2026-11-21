(() => {
  if (!("serviceWorker" in navigator)) return;
  const status = document.querySelector("[data-offline-status]");
  const list = document.querySelector("[data-offline-list]");
  const limitInput = document.querySelector("[data-offline-limit]");
  const storage = document.querySelector("[data-offline-storage]");
  const downloadButton = document.querySelector("[data-offline-download]");
  const puzzleId = downloadButton?.dataset.puzzleId || document.body.dataset.offlinePuzzleId || "";

  function hydrateOfflineProgress() {
    const node = document.getElementById("puzzary-offline-progress");
    if (!node) return;
    let progress = {};
    try { progress = JSON.parse(node.textContent || "{}"); } catch (_error) { return; }
    if (progress.answers && typeof progress.answers === "object") {
      document.querySelectorAll("[data-answers]").forEach(element => { element.dataset.answers = JSON.stringify(progress.answers); });
      const maze = document.querySelector(".maze-play");
      if (maze && progress.answers.path) {
        maze.dataset.path = progress.answers.path;
        maze.dataset.completed = progress.answers.completed === "true" ? "true" : "false";
      }
    }
    if (Array.isArray(progress.found_words)) document.querySelectorAll("[data-found-words]").forEach(element => { element.dataset.foundWords = progress.found_words.join(","); });
  }
  hydrateOfflineProgress();

  function setStatus(message, danger = false) {
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("danger", danger);
    status.hidden = !message;
  }

  async function worker() {
    const registration = await navigator.serviceWorker.ready;
    return navigator.serviceWorker.controller || registration.active;
  }

  async function request(type, data = {}) {
    const target = await worker();
    if (!target) throw new Error("Offline support is not ready yet. Refresh and try again.");
    return new Promise((resolve, reject) => {
      const channel = new MessageChannel();
      const timer = window.setTimeout(() => reject(new Error("The offline request timed out.")), 30000);
      channel.port1.onmessage = event => {
        window.clearTimeout(timer);
        event.data?.ok ? resolve(event.data.value) : reject(new Error(event.data?.error || "Offline request failed."));
      };
      target.postMessage({type, ...data}, [channel.port2]);
    });
  }

  const limit = () => Math.max(1, Math.min(Number(localStorage.getItem("puzzary-offline-limit")) || 20, 100));

  async function updateStorage() {
    if (!storage || !navigator.storage?.estimate) return;
    const estimate = await navigator.storage.estimate();
    const used = Number(estimate.usage || 0) / 1024 / 1024;
    const quota = Number(estimate.quota || 0) / 1024 / 1024;
    storage.textContent = quota ? `${used.toFixed(1)} MB of ${quota.toFixed(0)} MB browser storage used. Browsers may evict downloads when space is low.` : `${used.toFixed(1)} MB browser storage used.`;
  }

  function conflictActions(item) {
    if (!item.conflict) return "";
    const message = item.conflict.detail?.message || "Progress also changed on the server.";
    if (item.conflict.detail?.conflict === "puzzle_updated") return `<div class="notice danger"><p>${escapeHtml(message)}</p><button type="button" class="ghost-button compact-button" data-conflict="server" data-id="${escapeHtml(item.puzzleId)}">Remove old download</button></div>`;
    return `<div class="notice danger"><p>${escapeHtml(message)}</p><div class="action-row"><button type="button" class="ghost-button compact-button" data-conflict="server" data-id="${escapeHtml(item.puzzleId)}">Use server progress</button><button type="button" class="danger-button compact-button" data-conflict="device" data-id="${escapeHtml(item.puzzleId)}">Keep this device's progress</button></div></div>`;
  }

  function escapeHtml(value) {
    const node = document.createElement("span");
    node.textContent = String(value || "");
    return node.innerHTML;
  }

  async function render() {
    const items = await request("LIST_OFFLINE_PUZZLES");
    if (downloadButton) {
      const downloaded = items.some(item => item.puzzleId === puzzleId);
      downloadButton.textContent = downloaded ? "Remove offline download" : "Available offline";
      downloadButton.dataset.downloaded = String(downloaded);
    }
    if (list) {
      list.innerHTML = items.length ? items.map(item => `<article class="row offline-puzzle-row"><span><a href="${escapeHtml(item.canonicalUrl)}"><strong>${escapeHtml(item.title)}</strong></a><small>${escapeHtml(item.puzzleType.replaceAll("_", " "))} · saved ${new Date(item.downloadedAt).toLocaleString()}${item.pending ? " · sync pending" : ""}</small></span><button type="button" class="ghost-button compact-button" data-remove-offline="${escapeHtml(item.puzzleId)}">Remove</button>${conflictActions(item)}</article>`).join("") : "<p class=\"muted\">No puzzles are saved on this device.</p>";
    }
    await updateStorage();
    return items;
  }

  downloadButton?.addEventListener("click", async () => {
    downloadButton.disabled = true;
    try {
      if (downloadButton.dataset.downloaded === "true") {
        await request("REMOVE_OFFLINE_PUZZLE", {puzzleId});
        setStatus("Offline download removed.");
      } else {
        await request("DOWNLOAD_OFFLINE_PUZZLE", {puzzleId, limit: limit()});
        setStatus("Puzzle is ready offline. Progress will sync after reconnecting.");
      }
      await render();
    } catch (error) { setStatus(error.message, true); }
    finally { downloadButton.disabled = false; }
  });

  list?.addEventListener("click", async event => {
    const remove = event.target.closest("[data-remove-offline]");
    const conflict = event.target.closest("[data-conflict]");
    try {
      if (remove) { await request("REMOVE_OFFLINE_PUZZLE", {puzzleId: remove.dataset.removeOffline}); setStatus("Offline download removed."); }
      if (conflict) { await request("RESOLVE_OFFLINE_CONFLICT", {puzzleId: conflict.dataset.id, choice: conflict.dataset.conflict}); setStatus(conflict.dataset.conflict === "server" ? "Server progress kept." : "This device's progress synchronized."); }
      if (remove || conflict) await render();
    } catch (error) { setStatus(error.message, true); }
  });

  limitInput?.addEventListener("change", () => {
    const value = Math.max(1, Math.min(Number(limitInput.value) || 20, 100));
    limitInput.value = String(value);
    localStorage.setItem("puzzary-offline-limit", String(value));
    setStatus(`Offline puzzle limit set to ${value}.`);
  });
  if (limitInput) limitInput.value = String(limit());

  document.querySelector("[data-offline-sync]")?.addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    try { await request("SYNC_OFFLINE_PROGRESS"); setStatus("Offline progress sync finished."); await render(); }
    catch (error) { setStatus(error.message, true); }
    finally { event.currentTarget.disabled = false; }
  });

  navigator.serviceWorker.addEventListener("message", event => {
    const messages = {OFFLINE_PROGRESS_QUEUED: "Progress saved on this device. Server confirmation is pending.", OFFLINE_SYNCED: "Offline progress synchronized.", OFFLINE_CONFLICT: "Progress changed elsewhere. Review the conflict in Offline puzzles.", OFFLINE_REMOVED: event.data?.detail || "The offline copy was removed because access changed."};
    if (messages[event.data?.type]) { setStatus(messages[event.data.type], event.data.type === "OFFLINE_CONFLICT" || event.data.type === "OFFLINE_REMOVED"); render().catch(() => {}); }
  });

  render().catch(error => setStatus(error.message, true));
})();
