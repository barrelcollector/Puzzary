function pushApplicationKey(value) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const raw = atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (character) => character.charCodeAt(0));
}

const settings = document.querySelector("[data-notification-settings]");
if (settings) {
  const status = settings.querySelector("[data-notification-status]");
  const enable = settings.querySelector("[data-enable-notifications]");
  const deviceName = settings.querySelector("[data-device-name]");
  const supported = window.isSecureContext && "Notification" in window && "serviceWorker" in navigator && "PushManager" in window;

  if (!supported) {
    enable.disabled = true;
    status.textContent = window.isSecureContext
      ? "This browser does not support background notifications. In-app notifications remain available."
      : "Background notifications require HTTPS or localhost. In-app notifications remain available.";
  } else if (Notification.permission === "denied") {
    enable.disabled = true;
    status.textContent = "Notifications are blocked in this browser. Change the site permission to enable them; in-app notifications remain available.";
  } else if (Notification.permission === "granted") {
    status.textContent = "Browser permission is granted. Enable this browser to create or refresh its subscription.";
  }

  enable?.addEventListener("click", async () => {
    enable.disabled = true;
    status.textContent = "Requesting browser permission…";
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        status.textContent = permission === "denied"
          ? "Permission was denied. You can still enable the in-app notification list below."
          : "Permission was not granted. No browser subscription was created.";
        return;
      }
      const registration = await navigator.serviceWorker.register("/service-worker.js", { scope: "/" });
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: pushApplicationKey(settings.dataset.publicKey),
        });
      }
      const response = await fetch("/api/notifications/subscriptions", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...subscription.toJSON(),
          device_name: deviceName.value.trim() || "Browser",
        }),
      });
      if (!response.ok) throw new Error(`Subscription failed (${response.status})`);
      status.textContent = "This browser is enabled. Reloading device status…";
      window.location.reload();
    } catch (error) {
      status.textContent = `Could not enable this browser: ${error.message}`;
    } finally {
      if (Notification.permission !== "denied") enable.disabled = false;
    }
  });
}

const accountLink = document.querySelector("[data-account-link]");
if (accountLink && document.body.dataset.signedIn === "true") {
  fetch("/api/notifications", { credentials: "same-origin" })
    .then((response) => response.ok ? response.json() : null)
    .then((data) => {
      if (!data?.unread) return;
      const badge = document.createElement("span");
      badge.className = "notification-badge";
      badge.textContent = data.unread > 99 ? "99+" : String(data.unread);
      badge.setAttribute("aria-label", `${data.unread} unread notifications`);
      accountLink.append(badge);
    })
    .catch(() => {});
}
