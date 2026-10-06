(function () {
  const root = document.querySelector(".cross-equations-play");
  if (!root) return;

  const inputs = Array.from(root.querySelectorAll(".number-cell input[data-key]"));
  const status = document.getElementById("cross-equations-status");
  const checkButton = document.getElementById("cross-equations-check");
  const clearButton = document.getElementById("cross-equations-clear");
  const revealButton = document.getElementById("reveal-solution");
  const progressUrl = root.dataset.progressUrl;
  const valueMax = Number(root.dataset.valueMax || 9);
  const gridSize = Math.max(3, Number(root.dataset.gridSize || 3));
  const maxCoord = (gridSize - 1) * 2;
  let solution = {};
  let saveTimer = null;

  try {
    solution = JSON.parse(root.dataset.solution || "{}");
  } catch (_error) {
    solution = {};
  }
  const equations = JSON.parse(root.dataset.equations || '[]');
  const progressSounds = window.PuzzarySounds?.createProgressTracker(
    () => equations.flatMap((equation, index) => equation.cells.length && equation.cells.every(key => {
      const input = inputAt(key);
      return input && input.value.trim() === expectedFor(input);
    }) ? [index] : []),
    () => inputs.length > 0 && inputs.every(input => input.value.trim() === expectedFor(input))
  );

  function currentAnswers() {
    const answers = {};
    inputs.forEach((input) => {
      if (input.disabled) return;
      const value = input.value.trim();
      if (/^-?\d+$/.test(value)) answers[input.dataset.key] = value;
    });
    return answers;
  }

  function saveProgress() {
    if (window.PuzzaryGroup) { window.PuzzaryGroup.save(); return; }
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
  window.PuzzaryGroup?.register({read:currentAnswers, apply(state) {
    inputs.forEach(input => {
      if (!input.disabled) input.value = state.answers[input.dataset.key] || '';
      input.closest('.number-cell').classList.toggle('incorrect',(state.incorrect || []).includes(input.dataset.key));
    }); progressSounds?.sync();
  }});

  function inputAt(key) {
    return inputs.find((input) => input.dataset.key === key) || null;
  }

  function move(input, dx, dy) {
    const [xText, yText] = input.dataset.key.split(",");
    let x = Number(xText) + dx * 2;
    let y = Number(yText) + dy * 2;
    while (x >= 0 && x <= maxCoord && y >= 0 && y <= maxCoord) {
      const next = inputAt(`${x},${y}`);
      if (next && !next.disabled) {
        next.focus();
        next.select();
        return;
      }
      x += dx * 2;
      y += dy * 2;
    }
  }

  function setValue(input, value) {
    if (input.disabled) return;
    let text = String(value).replace(/[^0-9-]/g, "");
    if (text.includes("-")) {
      text = `${text.startsWith("-") ? "-" : ""}${text.replace(/-/g, "")}`;
    }
    if (/^-?\d+$/.test(text)) {
      const number = Number(text);
      if (number > valueMax) text = String(valueMax);
      if (number < -valueMax * valueMax) text = String(-valueMax * valueMax);
    }
    input.value = text.slice(0, 4);
    input.closest(".number-cell").classList.remove("correct", "incorrect");
    progressSounds?.invalidate();
    queueSave();
  }

  function expectedFor(input) {
    return String(solution[input.dataset.key] ?? "");
  }

  function checkGrid() {
    progressSounds?.check();
    let filled = 0;
    let correct = 0;
    inputs.forEach((input) => {
      const cell = input.closest(".number-cell");
      const value = input.value.trim();
      cell.classList.remove("correct", "incorrect");
      if (!value) return;
      filled += 1;
      if (value === expectedFor(input)) {
        correct += 1;
        cell.classList.add("correct");
      } else {
        cell.classList.add("incorrect");
      }
    });
    if (status) {
      status.textContent = correct === inputs.length
        ? "Cross Equations complete."
        : `${correct} of ${inputs.length} cells correct. ${filled} filled.`;
    }
    if (inputs.length > 0 && correct === inputs.length && window.PuzzaryCelebrate) {
      window.PuzzaryCelebrate.complete();
    }
  }

  function revealSolution() {
    inputs.forEach((input) => {
      input.value = expectedFor(input);
      input.closest(".number-cell").classList.remove("incorrect");
      input.closest(".number-cell").classList.add("correct");
    });
    if (status) status.textContent = "Solution revealed. Reset the puzzle to start over.";
    progressSounds?.sync();
    saveProgress();
  }

  inputs.forEach((input) => {
    input.addEventListener("input", () => {
      setValue(input, input.value);
    });
    input.addEventListener("keydown", (event) => {
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if (/^[0-9]$/.test(event.key)) {
        return;
      } else if (event.key === "Enter" || event.key === "Tab") {
        if (event.key === "Enter") {
          event.preventDefault();
          move(input, 1, 0);
        }
      } else if (event.key === "Backspace") {
        if (!input.value) {
          event.preventDefault();
          move(input, -1, 0);
        }
      } else if (event.key === "Delete") {
        return;
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
        input.closest(".number-cell").classList.remove("correct", "incorrect");
      });
      if (status) status.textContent = "Grid cleared.";
      progressSounds?.sync();
      saveProgress();
    });
  }
  if (revealButton) revealButton.addEventListener("click", revealSolution);
})();
