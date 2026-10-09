(function () {
  document.querySelectorAll("[data-completion-message-input]").forEach((input) => {
    const scope = input.closest("form") || document;
    const preview = input.parentElement?.nextElementSibling?.matches("[data-completion-message-preview]")
      ? input.parentElement.nextElementSibling
      : scope.querySelector("[data-completion-message-preview]");
    const text = preview?.querySelector("[data-completion-message-preview-text]");
    if (!text) return;
    const update = () => { text.textContent = input.value.trim() || "Nice solve."; };
    input.addEventListener("input", update);
    update();
  });
})();
