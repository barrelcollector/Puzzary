(function () {
  const root = document.querySelector(".sudoku-play");
  if (!root) return;

  const inputs = Array.from(root.querySelectorAll(".sudoku-cell input[data-key]"));
  const status = document.getElementById("sudoku-status");
  const checkButton = document.getElementById("sudoku-check");
  const clearButton = document.getElementById("sudoku-clear");
  const revealButton = document.getElementById("reveal-solution");
  const progressUrl = root.dataset.progressUrl;
  const size = Number(root.dataset.size || 9);
  const checkMode = root.dataset.checkMode || "check_puzzle";
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
      if (input.disabled) return;
      const value = input.value.trim().slice(0, 1);
      if (/^[1-9]$/.test(value)) answers[input.dataset.key] = value;
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
    return solution[y] && solution[y][x] ? String(solution[y][x]) : "";
  }

  function inputAt(x, y) {
    return inputs.find((input) => input.dataset.key === `${x},${y}`) || null;
  }

  function move(input, dx, dy) {
    const [xText, yText] = input.dataset.key.split(",");
    let x = Number(xText) + dx;
    let y = Number(yText) + dy;
    while (x >= 0 && x < size && y >= 0 && y < size) {
      const next = inputAt(x, y);
      if (next && !next.disabled) {
        next.focus();
        next.select();
        return;
      }
      x += dx;
      y += dy;
    }
  }

  function setValue(input, value) {
    if (input.disabled) return;
    const allowed = new RegExp(`[^1-${size}]`, "g");
    const digit = String(value).replace(allowed, "").slice(0, 1);
    input.value = digit;
    input.closest(".sudoku-cell").classList.remove("correct", "incorrect");
    queueSave();
  }

  function checkGrid() {
    if (checkMode === "none") {
      if (status) status.textContent = "Checking is off for this puzzle.";
      return;
    }
    let filled = 0;
    let correct = 0;
    inputs.forEach((input) => {
      const cell = input.closest(".sudoku-cell");
      const value = input.value.trim();
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
      if (checkMode === "check_cell") return;
    });
    if (status) {
      status.textContent = correct === inputs.length
        ? "Sudoku complete."
        : `${correct} of ${inputs.length} cells correct. ${filled} filled.`;
    }
  }

  function revealSolution() {
    inputs.forEach((input) => {
      input.value = expectedFor(input);
      input.closest(".sudoku-cell").classList.remove("incorrect");
      input.closest(".sudoku-cell").classList.add("correct");
    });
    if (status) status.textContent = "Solution revealed. Reset the puzzle to start over.";
    saveProgress();
  }

  inputs.forEach((input) => {
    input.addEventListener("input", () => {
      setValue(input, input.value);
      if (input.value) move(input, 1, 0);
    });
    input.addEventListener("keydown", (event) => {
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      const digitPattern = new RegExp(`^[1-${size}]$`);
      if (digitPattern.test(event.key)) {
        event.preventDefault();
        setValue(input, event.key);
        move(input, 1, 0);
      } else if (event.key === "Backspace") {
        event.preventDefault();
        if (input.value) {
          setValue(input, "");
        } else {
          move(input, -1, 0);
        }
      } else if (event.key === "Delete" || event.key === "0") {
        event.preventDefault();
        setValue(input, "");
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        move(input, 1, 0);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        move(input, -1, 0);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        move(input, 0, 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        move(input, 0, -1);
      }
    });
  });

  if (checkButton) checkButton.addEventListener("click", checkGrid);
  if (clearButton) {
    clearButton.addEventListener("click", () => {
      inputs.forEach((input) => {
        if (input.disabled) return;
        input.value = "";
        input.closest(".sudoku-cell").classList.remove("correct", "incorrect");
      });
      if (status) status.textContent = "Grid cleared.";
      saveProgress();
    });
  }
  if (revealButton) revealButton.addEventListener("click", revealSolution);
})();
