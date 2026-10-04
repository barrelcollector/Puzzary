(function () {
  const root = document.querySelector(".maze-play");
  if (!root) return;

  const progressUrl = root.dataset.progressUrl;
  const status = document.getElementById("maze-status");
  const completeButton = document.getElementById("maze-complete");
  const clearButton = document.getElementById("maze-clear");

  function setCompleted(completed) {
    root.dataset.completed = completed ? "true" : "false";
    root.classList.toggle("complete", completed);
    if (status) {
      status.textContent = completed ? "Maze marked complete." : "Completion cleared.";
    }
  }

  async function save(completed) {
    setCompleted(completed);
    if (!progressUrl) return;
    try {
      await fetch(progressUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answers: { completed: completed ? "true" : "" } }),
      });
    } catch (_error) {
      if (status) status.textContent = "Progress could not be saved.";
    }
  }

  setCompleted(root.dataset.completed === "true");
  if (completeButton) completeButton.addEventListener("click", () => save(true));
  if (clearButton) clearButton.addEventListener("click", () => save(false));
})();
