self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = { title: "New Puzzary puzzle", body: "A new puzzle is available.", url: "/library" };
  }
  const title = payload.title || "New Puzzary puzzle";
  event.waitUntil(self.registration.showNotification(title, {
    body: payload.body || "A new puzzle is available.",
    tag: payload.tag || "puzzary-new-puzzle",
    data: { url: payload.url || "/library", notificationId: payload.notification_id || "" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/library", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const windowClient of windows) {
      if ("navigate" in windowClient) await windowClient.navigate(target);
      return windowClient.focus();
    }
    return clients.openWindow(target);
  })());
});
