(function () {
  document.querySelectorAll("[data-publish-at-input]").forEach((input) => {
    const preview = input.closest("label")?.querySelector("[data-publish-at-preview]");
    const form = input.closest("form");
    const archiveInput = form?.querySelector('input[name="auto_archive_at"]');
    let managedArchive = Boolean(archiveInput && archiveInput.value);
    archiveInput?.addEventListener("input", () => { managedArchive = false; });
    if (!preview) return;
    const render = () => {
      if (!input.value) {
        preview.textContent = "Publishes immediately.";
        return;
      }
      const timezone = input.dataset.timezone || document.documentElement.dataset.timezone || "the configured timezone";
      preview.textContent = `Scheduled for ${input.value.replace("T", " ")} (${timezone}).`;
      if (managedArchive && archiveInput) {
        // Treat datetime-local as a wall-clock value in the configured server timezone,
        // independent of the browser machine's timezone.
        const release = new Date(`${input.value}:00Z`);
        if (!Number.isNaN(release.valueOf())) {
          const archive = new Date(release.valueOf() + (24 * 60 * 60 * 1000));
          archiveInput.value = archive.toISOString().slice(0, 16);
        }
      }
    };
    input.addEventListener("input", render);
    render();
  });
})();
