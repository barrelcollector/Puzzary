document.querySelectorAll("[data-copy-feed]").forEach((button) => {
  button.addEventListener("click", async () => {
    const input = button.closest(".feed-copy-row")?.querySelector("[data-feed-url]");
    const status = button.closest(".feed-secret")?.querySelector("[data-copy-status]");
    if (!input) return;
    try {
      await navigator.clipboard.writeText(input.value);
      if (status) status.textContent = "Feed URL copied.";
    } catch (_error) {
      input.focus();
      input.select();
      if (status) status.textContent = "Copy was blocked. The URL is selected for manual copying.";
    }
  });
});
