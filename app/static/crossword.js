(function () {
  const root = document.querySelector(".crossword-play");
  if (!root) return;

  const inputs = Array.from(root.querySelectorAll(".crossword-cell input[data-key]"));
  const status = document.getElementById("crossword-status");
  const checkButton = document.getElementById("crossword-check");
  const clearButton = document.getElementById("crossword-clear");
  const progressUrl = root.dataset.progressUrl;
  let solution = [];
  let saveTimer = null;

  try {
    solution = JSON.parse(root.dataset.solution || "[]");
  } catch (_error) {
    solution = [];
  }

  function currentAnswers() {
    const answers = {};
    inputs.forEach((input) => {
      const value = input.value.trim().toUpperCase().slice(0, 1);
      if (value) answers[input.dataset.key] = value;
    });
    return answers;
  }

  function saveProgress() {
    if (!progressUrl) return;
    window.fetch(progressUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers: currentAnswers() }),
    }).catch(() => {
      if (status) status.textContent = "Progress could not be saved.";
    });
  }

  function queueSave() {
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(saveProgress, 250);
  }

  function expectedFor(input) {
    const [xText, yText] = input.dataset.key.split(",");
    const x = Number(xText);
    const y = Number(yText);
    return solution[y] && solution[y][x] ? String(solution[y][x]).toUpperCase() : "";
  }

  function checkGrid() {
    let filled = 0;
    let correct = 0;
    inputs.forEach((input) => {
      const cell = input.closest(".crossword-cell");
      const value = input.value.trim().toUpperCase();
      const expected = expectedFor(input);
      cell.classList.remove("correct", "incorrect");
      if (!value) return;
      filled += 1;
      if (value === expected) {
        correct += 1;
        cell.classList.add("correct");
      } else {
        cell.classList.add("incorrect");
      }
    });
    if (status) {
      status.textContent = correct === inputs.length
        ? "Crossword complete."
        : `${correct} of ${inputs.length} letters correct.`;
    }
  }

  function moveFrom(input, direction) {
    const index = inputs.indexOf(input);
    const next = inputs[index + direction];
    if (next) next.focus();
  }

  inputs.forEach((input) => {
    input.addEventListener("input", () => {
      input.value = input.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 1);
      input.closest(".crossword-cell").classList.remove("correct", "incorrect");
      if (input.value) moveFrom(input, 1);
      queueSave();
    });

    input.addEventListener("keydown", (event) => {
      if (event.key === "Backspace" && !input.value) {
        event.preventDefault();
        moveFrom(input, -1);
      }
    });
  });

  if (checkButton) {
    checkButton.addEventListener("click", checkGrid);
  }

  if (clearButton) {
    clearButton.addEventListener("click", () => {
      inputs.forEach((input) => {
        input.value = "";
        input.closest(".crossword-cell").classList.remove("correct", "incorrect");
      });
      if (status) status.textContent = "Grid cleared.";
      saveProgress();
    });
  }
})();
