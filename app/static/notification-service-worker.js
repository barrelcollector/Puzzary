const SHELL_CACHE = "puzzary-shell-v3-__PUZZARY_VERSION__";
const SHELL_PREFIX = "puzzary-shell-";
const USER_CACHE_PREFIX = "puzzary-user-";
const OFFLINE_FALLBACK = "/static/offline.html?v=2";
const DB_NAME = "puzzary-offline-v1";
const SHELL_ASSETS = [OFFLINE_FALLBACK, "/static/styles.css?v=55", "/static/navigation.js?v=1", "/static/pwa.js?v=2", "/static/offline-puzzles.js?v=2", "/static/brand/puzzary-mark.svg?v=1", "/static/brand/puzzary-192.png?v=1", "/static/brand/puzzary-512.png?v=1"];

function database() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      for (const name of ["puzzles", "queue", "conflicts", "meta"])
        if (!request.result.objectStoreNames.contains(name)) request.result.createObjectStore(name, {keyPath: name === "meta" ? "key" : "puzzleId"});
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function storeOperation(name, mode, operation) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(name, mode), store = transaction.objectStore(name);
    let request;
    try { request = operation(store); } catch (error) { reject(error); return; }
    transaction.oncomplete = () => resolve(request?.result);
    transaction.onerror = () => reject(transaction.error);
  });
}
const getRecord = (name, key) => storeOperation(name, "readonly", store => store.get(key));
const putRecord = (name, value) => storeOperation(name, "readwrite", store => store.put(value));
const deleteRecord = (name, key) => storeOperation(name, "readwrite", store => store.delete(key));
const allRecords = name => storeOperation(name, "readonly", store => store.getAll());
const activeScope = async () => (await getRecord("meta", "activeScope"))?.value || "anonymous";
const userCache = scope => `${USER_CACHE_PREFIX}${String(scope).replace(/[^a-zA-Z0-9_-]/g, "_")}`;

async function notifyClients(message) {
  (await self.clients.matchAll({type: "window", includeUncontrolled: true})).forEach(client => client.postMessage(message));
}

self.addEventListener("install", event => event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(SHELL_ASSETS))));
self.addEventListener("activate", event => event.waitUntil((async () => {
  const names = await caches.keys();
  await Promise.all(names.filter(name => name.startsWith(SHELL_PREFIX) && name !== SHELL_CACHE).map(name => caches.delete(name)));
  await self.clients.claim();
})()));

async function clearUserData() {
  await Promise.all((await caches.keys()).filter(name => name.startsWith(USER_CACHE_PREFIX)).map(name => caches.delete(name)));
  for (const name of ["puzzles", "queue", "conflicts"]) await storeOperation(name, "readwrite", store => store.clear());
}

async function removePuzzle(puzzleId) {
  const record = await getRecord("puzzles", puzzleId);
  if (record) await (await caches.open(userCache(record.scope))).delete(new URL(record.canonicalUrl, self.location.origin).href);
  await Promise.all([deleteRecord("puzzles", puzzleId), deleteRecord("queue", puzzleId), deleteRecord("conflicts", puzzleId)]);
}

async function downloadPuzzle(puzzleId, limit) {
  const scope = await activeScope();
  if (scope === "anonymous") throw new Error("Sign in before saving puzzles offline.");
  const response = await fetch(`/api/puzzles/${encodeURIComponent(puzzleId)}/offline-package`, {credentials: "include"});
  const packageData = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof packageData.detail === "string" ? packageData.detail : "This puzzle cannot be downloaded.");
  const existing = await getRecord("puzzles", puzzleId);
  const records = (await allRecords("puzzles") || []).filter(item => item.scope === scope);
  if (!existing && records.length >= Math.max(1, Math.min(Number(limit) || 20, 100))) throw new Error("Your offline puzzle limit is reached. Remove a download or raise the limit.");
  const page = await fetch(packageData.download_url, {credentials: "include", cache: "no-store"});
  if (!page.ok) throw new Error("The offline puzzle page could not be prepared.");
  const manager = await fetch("/offline", {credentials: "include", cache: "no-store"});
  let bytes = Number(page.headers.get("content-length") || 0);
  const assets = [];
  for (const asset of packageData.assets || []) {
    const assetResponse = await fetch(asset, {credentials: "include", cache: "no-store"});
    if (!assetResponse.ok) throw new Error(`A required puzzle asset could not be downloaded: ${asset}`);
    bytes += Number(assetResponse.headers.get("content-length") || 0);
    assets.push([asset, assetResponse]);
  }
  const cache = await caches.open(userCache(scope)), canonical = new URL(packageData.canonical_url, self.location.origin).href;
  await cache.put(canonical, page.clone());
  if (manager.ok) await cache.put(new URL("/offline", self.location.origin).href, manager.clone());
  for (const [asset, assetResponse] of assets) await cache.put(asset, assetResponse.clone());
  const record = {puzzleId, scope, title: packageData.title, puzzleType: packageData.puzzle_type, canonicalUrl: packageData.canonical_url, puzzleVersion: packageData.puzzle_version, progress: packageData.progress, progressFields: packageData.offline_progress_fields || [], validation: packageData.offline_validation || "server", downloadedAt: new Date().toISOString(), bytes};
  await putRecord("puzzles", record);
  await deleteRecord("conflicts", puzzleId);
  return record;
}

async function queueProgress(request, puzzleId) {
  const scope = await activeScope(), puzzle = await getRecord("puzzles", puzzleId);
  if (!puzzle || puzzle.scope !== scope) throw new Error("This puzzle was not saved for offline use.");
  const body = await request.json(), permitted = {};
  for (const field of puzzle.progressFields || []) if (Object.hasOwn(body, field)) permitted[field] = body[field];
  await putRecord("queue", {puzzleId, scope, payload: permitted, baseRevision: puzzle.progress?.revision || "none", puzzleVersion: puzzle.puzzleVersion, queuedAt: new Date().toISOString()});
  self.registration.sync?.register("puzzary-progress-sync").catch(() => {});
  puzzle.progress = {...puzzle.progress, ...permitted};
  await putRecord("puzzles", puzzle);
  await notifyClients({type: "OFFLINE_PROGRESS_QUEUED", puzzleId});
  return new Response(JSON.stringify({...puzzle.progress, queued: true, pending_server_confirmation: true}), {status: 202, headers: {"Content-Type": "application/json", "X-Puzzary-Offline": "queued"}});
}

async function handleProgress(request, puzzleId) {
  const offlineCopy = request.clone();
  try {
    const response = await fetch(request);
    if (response.ok) {
      const data = await response.clone().json().catch(() => null), record = await getRecord("puzzles", puzzleId);
      if (record && data) { record.progress = data; await putRecord("puzzles", record); }
    }
    return response;
  } catch (_error) { return queueProgress(offlineCopy, puzzleId); }
}

async function syncOne(item, force = false) {
  const response = await fetch(`/api/puzzles/${encodeURIComponent(item.puzzleId)}/offline-sync`, {method: "POST", credentials: "include", headers: {"Content-Type": "application/json"}, body: JSON.stringify({base_revision: item.baseRevision, puzzle_version: item.puzzleVersion, progress: item.payload, force})});
  const result = await response.json().catch(() => ({}));
  if (response.ok) {
    const record = await getRecord("puzzles", item.puzzleId);
    if (record) { record.progress = result.progress; record.puzzleVersion = result.puzzle_version; await putRecord("puzzles", record); }
    await Promise.all([deleteRecord("queue", item.puzzleId), deleteRecord("conflicts", item.puzzleId)]);
    await notifyClients({type: "OFFLINE_SYNCED", puzzleId: item.puzzleId});
    return {status: "synced"};
  }
  if (response.status === 409) {
    const detail = result.detail || result;
    await putRecord("conflicts", {puzzleId: item.puzzleId, scope: item.scope, detail, createdAt: new Date().toISOString()});
    await notifyClients({type: "OFFLINE_CONFLICT", puzzleId: item.puzzleId, detail});
    return {status: "conflict", detail};
  }
  if ([401, 403, 404, 410, 423].includes(response.status)) {
    await removePuzzle(item.puzzleId);
    await notifyClients({type: "OFFLINE_REMOVED", puzzleId: item.puzzleId, detail: result.detail || "Puzzle access is no longer available."});
    return {status: "removed"};
  }
  throw new Error(result.detail || "Progress sync failed.");
}

async function syncPending() {
  const scope = await activeScope(), items = (await allRecords("queue") || []).filter(item => item.scope === scope), results = [];
  for (const item of items) try { results.push(await syncOne(item)); } catch (error) { results.push({status: "error", puzzleId: item.puzzleId, message: error.message}); }
  await notifyClients({type: "OFFLINE_SYNC_COMPLETE", results});
  return results;
}

async function offlineState() {
  const scope = await activeScope();
  const [puzzles, queue, conflicts] = await Promise.all([allRecords("puzzles"), allRecords("queue"), allRecords("conflicts")]);
  const pending = new Map((queue || []).filter(item => item.scope === scope).map(item => [item.puzzleId, item]));
  const conflict = new Map((conflicts || []).filter(item => item.scope === scope).map(item => [item.puzzleId, item]));
  return (puzzles || []).filter(item => item.scope === scope).map(item => ({...item, pending: pending.get(item.puzzleId) || null, conflict: conflict.get(item.puzzleId) || null}));
}

async function hydratedOfflinePage(response, pathname) {
  const match = pathname.match(/^\/puzzles\/([^/]+)$/);
  if (!match) return response;
  const record = await getRecord("puzzles", decodeURIComponent(match[1]));
  if (!record) return response;
  const text = await response.text();
  const state = JSON.stringify({found_words: record.progress?.found_words || [], answers: record.progress?.answers || {}}).replace(/</g, "\\u003c");
  const hydrated = text.replace(/(<script type="application\/json" id="puzzary-offline-progress">)[\s\S]*?(<\/script>)/, `$1${state}$2`);
  const headers = new Headers(response.headers);
  headers.delete("Content-Encoding");
  headers.delete("Content-Length");
  return new Response(hydrated, {status: response.status, statusText: response.statusText, headers});
}

self.addEventListener("message", event => {
  const data = event.data || {}, reply = (value, error = "") => event.ports?.[0]?.postMessage({ok: !error, value, error});
  if (data.type === "SKIP_WAITING") self.skipWaiting();
  if (data.type === "SET_USER_SCOPE") event.waitUntil(putRecord("meta", {key: "activeScope", value: String(data.scope || "anonymous")}));
  if (data.type === "CLEAR_USER_DATA") event.waitUntil(clearUserData());
  if (data.type === "DOWNLOAD_OFFLINE_PUZZLE") event.waitUntil(downloadPuzzle(data.puzzleId, data.limit).then(reply).catch(error => reply(null, error.message)));
  if (data.type === "REMOVE_OFFLINE_PUZZLE") event.waitUntil(removePuzzle(data.puzzleId).then(() => reply(true)).catch(error => reply(null, error.message)));
  if (data.type === "LIST_OFFLINE_PUZZLES") event.waitUntil(offlineState().then(reply).catch(error => reply(null, error.message)));
  if (data.type === "SYNC_OFFLINE_PROGRESS") event.waitUntil(syncPending().then(reply).catch(error => reply(null, error.message)));
  if (data.type === "RESOLVE_OFFLINE_CONFLICT") event.waitUntil((async () => {
    const item = await getRecord("queue", data.puzzleId), conflict = await getRecord("conflicts", data.puzzleId);
    if (conflict?.detail?.conflict === "puzzle_updated") {
      await removePuzzle(data.puzzleId);
      return {status: "removed"};
    }
    if (data.choice === "server") {
      const record = await getRecord("puzzles", data.puzzleId), serverProgress = conflict?.detail?.server_progress;
      if (record && serverProgress) { record.progress = serverProgress; await putRecord("puzzles", record); }
      await Promise.all([deleteRecord("queue", data.puzzleId), deleteRecord("conflicts", data.puzzleId)]);
      return {status: "server"};
    }
    if (!item) throw new Error("The pending progress is no longer available.");
    return syncOne(item, true);
  })().then(reply).catch(error => reply(null, error.message)));
});

function cacheableStatic(url) { return url.pathname.startsWith("/static/") || /^\/plugins\/[^/]+\/static\//.test(url.pathname); }
async function staticResponse(request) {
  const scope = await activeScope(), userCached = scope === "anonymous" ? null : await (await caches.open(userCache(scope))).match(request), shell = await caches.open(SHELL_CACHE), cached = userCached || await shell.match(request);
  const network = fetch(request).then(response => { if (response.ok && response.type === "basic" && !/private|no-store/i.test(response.headers.get("Cache-Control") || "")) shell.put(request, response.clone()); return response; });
  if (new URL(request.url).pathname.startsWith("/plugins/")) try { return await network; } catch (_error) { return cached || Response.error(); }
  return cached || network;
}

self.addEventListener("fetch", event => {
  const request = event.request, url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  const progressMatch = request.method === "POST" && url.pathname.match(/^\/api\/puzzles\/([^/]+)\/progress$/);
  if (progressMatch) { event.respondWith(handleProgress(request, decodeURIComponent(progressMatch[1]))); return; }
  if (request.method !== "GET") return;
  if (request.mode === "navigate") {
    event.respondWith((async () => { try { return await fetch(request); } catch (_error) {
      const scope = await activeScope();
      if (scope !== "anonymous") { const cached = await (await caches.open(userCache(scope))).match(new URL(url.pathname, self.location.origin).href); if (cached) return hydratedOfflinePage(cached, url.pathname); }
      return caches.match(OFFLINE_FALLBACK);
    } })());
    return;
  }
  if (cacheableStatic(url)) event.respondWith(staticResponse(request));
});
self.addEventListener("sync", event => { if (event.tag === "puzzary-progress-sync") event.waitUntil(syncPending()); });

self.addEventListener("push", event => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_error) { payload = {title: "New Puzzary puzzle", body: "A new puzzle is available.", url: "/library"}; }
  event.waitUntil(self.registration.showNotification(payload.title || "New Puzzary puzzle", {body: payload.body || "A new puzzle is available.", tag: payload.tag || "puzzary-new-puzzle", data: {url: payload.url || "/library", notificationId: payload.notification_id || ""}, icon: "/static/brand/puzzary-192.png?v=1", badge: "/static/brand/puzzary-48.png?v=1"}));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/library", self.location.origin).href;
  event.waitUntil((async () => { for (const windowClient of await clients.matchAll({type: "window", includeUncontrolled: true})) { if ("navigate" in windowClient) await windowClient.navigate(target); return windowClient.focus(); } return clients.openWindow(target); })());
});
