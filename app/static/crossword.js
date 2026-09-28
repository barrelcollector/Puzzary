(function () {
  const root = document.querySelector(".crossword-play");
  if (!root) return;

  const inputs = Array.from(root.querySelectorAll(".crossword-cell input[data-key]"));
  const status = document.getElementById("crossword-status");
  const checkButton = document.getElementById("crossword-check");
  const clearButton = document.getElementById("crossword-clear");
  const revealButton = document.getElementById("reveal-solution");
  const progressUrl = root.dataset.progressUrl;
  let solution = [];
  let entries = [];
  let saveTimer = null;
  let activeEntry = null;

  try {
    solution = JSON.parse(root.dataset.solution || "[]");
  } catch (_error) {
    solution = [];
  }

  try {
    entries = JSON.parse(root.dataset.entries || "[]");
  } catch (_error) {
    entries = [];
  }

  const inputsByKey = new Map(inputs.map((input) => [input.dataset.key, input]));
  const entriesByCell = new Map();
  entries.forEach((entry) => {
    const dx = entry.direction === "across" ? 1 : 0;
    const dy = entry.direction === "down" ? 1 : 0;
    const answer = String(entry.answer || "");
    for (let index = 0; index < answer.length; index += 1) {
      const key = `${Number(entry.x) + dx * index},${Number(entry.y) + dy * index}`;
      const list = entriesByCell.get(key) || [];
      list.push(entry);
      entriesByCell.set(key, list);
    }
  });

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

  function revealSolution() {
    inputs.forEach((input) => {
      input.value = expectedFor(input);
      input.closest(".crossword-cell").classList.remove("incorrect");
      input.closest(".crossword-cell").classList.add("correct");
    });
    if (status) status.textContent = "Solution revealed. Reset the puzzle to start over.";
  }

  function chooseEntry(input, allowToggle) {
    const candidates = entriesByCell.get(input.dataset.key) || [];
    if (!candidates.length) {
      activeEntry = null;
      return null;
    }
    if (activeEntry && candidates.some((entry) => entry.number === activeEntry.number && entry.direction === activeEntry.direction)) {
      if (allowToggle && candidates.length > 1) {
        activeEntry = candidates.find((entry) => entry.direction !== activeEntry.direction) || activeEntry;
      }
      return activeEntry;
    }
    activeEntry = candidates.find((entry) => entry.direction === "across") || candidates[0];
    return activeEntry;
  }

  function nextInputInEntry(input, direction) {
    const entry = chooseEntry(input, false);
    if (!entry) return null;
    const [xText, yText] = input.dataset.key.split(",");
    const x = Number(xText);
    const y = Number(yText);
    const dx = entry.direction === "across" ? 1 : 0;
    const dy = entry.direction === "down" ? 1 : 0;
    return inputsByKey.get(`${x + dx * direction},${y + dy * direction}`) || null;
  }

  function moveFrom(input, direction) {
    const next = nextInputInEntry(input, direction);
    if (next) next.focus();
  }

  inputs.forEach((input) => {
    input.addEventListener("focus", () => {
      chooseEntry(input, false);
    });

    input.addEventListener("click", () => {
      chooseEntry(input, true);
    });

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

  if (revealButton) {
    revealButton.addEventListener("click", revealSolution);
  }
})();
