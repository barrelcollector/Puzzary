(function () {
  const root = document.querySelector(".crossword-play");
  if (!root) return;

  const inputs = Array.from(root.querySelectorAll(".crossword-cell input[data-key]"));
  const status = document.getElementById("crossword-status");
  const checkButton = document.getElementById("crossword-check");
  const clearButton = document.getElementById("crossword-clear");
  const revealButton = document.getElementById("reveal-solution");
  const revealEntryButtons = Array.from(document.querySelectorAll(".reveal-entry"));
  const checkEntryButtons = Array.from(document.querySelectorAll(".check-entry"));
  const moreClueButtons = Array.from(document.querySelectorAll(".more-clue"));
  const progressUrl = root.dataset.progressUrl;
  let solution = [];
  let entries = [];
  let settings = {};
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

  try {
    settings = JSON.parse(root.dataset.settings || "{}");
  } catch (_error) {
    settings = {};
  }

  const inputsByKey = new Map(inputs.map((input) => [input.dataset.key, input]));
  const entriesByCell = new Map();
  const cellsByEntry = new Map();
  const entryId = (entry) => `${entry.number}:${entry.direction}`;
  entries.forEach((entry) => {
    const dx = entry.direction === "across" ? 1 : 0;
    const dy = entry.direction === "down" ? 1 : 0;
    const answer = String(entry.answer || "");
    const keys = [];
    for (let index = 0; index < answer.length; index += 1) {
      const key = `${Number(entry.x) + dx * index},${Number(entry.y) + dy * index}`;
      keys.push(key);
      const list = entriesByCell.get(key) || [];
      list.push(entry);
      entriesByCell.set(key, list);
    }
    cellsByEntry.set(entryId(entry), keys);
  });
  const progressSounds = window.PuzzarySounds?.createProgressTracker(
    () => entries.filter(entry => entryValues(entry).join("") === String(entry.answer || "").toUpperCase()).map(entryId),
    () => inputs.length > 0 && inputs.every(input => input.value.trim().toUpperCase() === expectedFor(input))
  );

  function currentAnswers() {
    const answers = {};
    inputs.forEach((input) => {
      const value = input.value.trim().toUpperCase().slice(0, 1);
      if (value) answers[input.dataset.key] = value;
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
  window.PuzzaryGroup?.register({read() {
    return Object.fromEntries(inputs.filter(input => !input.disabled && input.value.trim())
      .map(input => [input.dataset.key,input.value.trim().toUpperCase()]));
  }, apply(state) {
    inputs.forEach(input => {
      const value = state.answers[input.dataset.key] || '';
      if (!input.disabled && input.value !== value) input.value = value;
      input.closest('.crossword-cell').classList.toggle('incorrect',(state.incorrect || []).includes(input.dataset.key));
    });
    const highlighted = new Set(settings.auto_highlight_correct ? entries.filter(entry => entryValues(entry).join('') === String(entry.answer || '').toUpperCase())
      .flatMap(entry => cellsByEntry.get(entryId(entry)) || []) : []);
    inputs.forEach(input => input.closest('.crossword-cell').classList.toggle('correct',
      !!input.value && input.value === expectedFor(input) && !!(state.checked || state.revealed || (state.checked_keys || []).includes(input.dataset.key) || highlighted.has(input.dataset.key))));
    progressSounds?.sync();
    moreClueButtons.forEach(button => {
      const count = state.clues?.[`${button.dataset.entryNumber}:${button.dataset.entryDirection}`] || 0;
      const list = button.nextElementSibling, steps = [...list.querySelectorAll('li')];
      list.hidden = !count; steps.forEach((step,i) => { step.hidden = i >= count; });
      button.disabled = count >= steps.length;
      button.textContent = button.disabled ? 'No more clues' : count ? `More clues (${steps.length-count})` : 'More clues';
    });
  }});

  function expectedFor(input) {
    const [xText, yText] = input.dataset.key.split(",");
    const x = Number(xText);
    const y = Number(yText);
    return solution[y] && solution[y][x] ? String(solution[y][x]).toUpperCase() : "";
  }

  function checkGrid() {
    progressSounds?.check();
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
    if (inputs.length > 0 && correct === inputs.length && window.PuzzaryCelebrate) {
      window.PuzzaryCelebrate.complete();
    }
  }

  function entryValues(entry) {
    const keys = cellsByEntry.get(entryId(entry)) || [];
    return keys.map((key) => {
      const input = inputsByKey.get(key);
      return input ? input.value.trim().toUpperCase() : "";
    });
  }

  function checkEntry(entry) {
    progressSounds?.check([entryId(entry)]);
    const keys = cellsByEntry.get(entryId(entry)) || [];
    const expected = String(entry.answer || "").toUpperCase();
    const values = entryValues(entry);
    const filled = values.every(Boolean);
    const correct = values.join("") === expected;
    keys.forEach((key, index) => {
      const input = inputsByKey.get(key);
      if (!input) return;
      const cell = input.closest(".crossword-cell");
      cell.classList.remove("correct", "incorrect");
      if (!values[index]) return;
      cell.classList.add(correct ? "correct" : "incorrect");
    });
    if (status) {
      status.textContent = correct
        ? `${entry.number} ${entry.direction} is correct.`
        : filled
          ? `${entry.number} ${entry.direction} needs another look.`
          : `${entry.number} ${entry.direction} is not filled yet.`;
    }
    return correct;
  }

  function autoHighlightEntries() {
    if (!settings.auto_highlight_correct) return;
    progressSounds?.check();
    entries.forEach((entry) => {
      const expected = String(entry.answer || "").toUpperCase();
      const values = entryValues(entry);
      if (values.every(Boolean) && values.join("") === expected) {
        (cellsByEntry.get(entryId(entry)) || []).forEach((key) => {
          const input = inputsByKey.get(key);
          if (input) input.closest(".crossword-cell").classList.add("correct");
        });
      }
    });
  }

  function revealSolution() {
    inputs.forEach((input) => {
      input.value = expectedFor(input);
      input.closest(".crossword-cell").classList.remove("incorrect");
      input.closest(".crossword-cell").classList.add("correct");
    });
    if (status) status.textContent = "Solution revealed. Reset the puzzle to start over.";
    progressSounds?.sync();
    saveProgress();
  }

  function revealEntry(entry) {
    const keys = cellsByEntry.get(entryId(entry)) || [];
    const revealMode = settings.reveal_mode || "word";
    const targetKeys = revealMode === "letter"
      ? keys.filter((key) => {
          const input = inputsByKey.get(key);
          return input && !input.value.trim();
        }).slice(0, 1)
      : keys;
    targetKeys.forEach((key) => {
      const input = inputsByKey.get(key);
      if (!input) return;
      const index = keys.indexOf(key);
      input.value = String(entry.answer || "")[index] || expectedFor(input);
      input.closest(".crossword-cell").classList.remove("incorrect");
      input.closest(".crossword-cell").classList.add("correct");
    });
    if (status) status.textContent = revealMode === "letter" ? `One letter revealed for ${entry.number} ${entry.direction}.` : `${entry.number} ${entry.direction} revealed.`;
    progressSounds?.sync();
    saveProgress();
  }

  function entryContainsInput(entry, input) {
    return (cellsByEntry.get(entryId(entry)) || []).includes(input.dataset.key);
  }

  function chooseEntry(input, options = {}) {
    const allowToggle = Boolean(options.allowToggle);
    const preferredDirection = options.preferredDirection || "";
    const candidates = entriesByCell.get(input.dataset.key) || [];
    if (!candidates.length) {
      activeEntry = null;
      return null;
    }
    if (preferredDirection) {
      const preferredEntry = candidates.find((entry) => entry.direction === preferredDirection);
      if (preferredEntry) {
        activeEntry = preferredEntry;
        return activeEntry;
      }
    }
    if (activeEntry && candidates.some((entry) => entryId(entry) === entryId(activeEntry))) {
      if (allowToggle && candidates.length > 1) {
        activeEntry = candidates.find((entry) => entry.direction !== activeEntry.direction) || activeEntry;
      }
      return activeEntry;
    }
    activeEntry = candidates.find((entry) => entry.direction === preferredDirection)
      || (activeEntry ? candidates.find((entry) => entry.direction === activeEntry.direction) : null)
      || candidates.find((entry) => entry.direction === "across")
      || candidates[0];
    return activeEntry;
  }

  function nextInputInEntry(input, direction) {
    const entry = activeEntry && entryContainsInput(activeEntry, input) ? activeEntry : chooseEntry(input);
    if (!entry) return null;
    const keys = cellsByEntry.get(entryId(entry)) || [];
    const index = keys.indexOf(input.dataset.key);
    if (index < 0) return null;
    for (let nextIndex = index + direction; nextIndex >= 0 && nextIndex < keys.length; nextIndex += direction) {
      const next = inputsByKey.get(keys[nextIndex]);
      if (next && !next.disabled) return next;
    }
    return null;
  }

  function moveFrom(input, direction) {
    const next = nextInputInEntry(input, direction);
    if (next) next.focus();
  }

  function setCellValue(input, value) {
    if (input.disabled) return;
    input.value = value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 1);
    input.closest(".crossword-cell").classList.remove("correct", "incorrect");
    progressSounds?.invalidate();
    queueSave();
    window.setTimeout(autoHighlightEntries, 0);
  }

  inputs.forEach((input) => {
    input.addEventListener("focus", () => {
      chooseEntry(input);
      input.select();
    });

    input.addEventListener("click", () => {
      chooseEntry(input, { allowToggle: true });
      input.select();
    });

    input.addEventListener("input", () => {
      setCellValue(input, input.value);
      if (input.value) moveFrom(input, 1);
    });

    input.addEventListener("keydown", (event) => {
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if (/^[a-zA-Z0-9]$/.test(event.key)) {
        event.preventDefault();
        setCellValue(input, event.key);
        moveFrom(input, 1);
        return;
      }
      if (event.key === "Backspace" && !input.value) {
        event.preventDefault();
        moveFrom(input, -1);
      } else if (event.key === "Backspace") {
        event.preventDefault();
        setCellValue(input, "");
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        chooseEntry(input, { preferredDirection: "across" });
        moveFrom(input, 1);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        chooseEntry(input, { preferredDirection: "across" });
        moveFrom(input, -1);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        chooseEntry(input, { preferredDirection: "down" });
        moveFrom(input, 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        chooseEntry(input, { preferredDirection: "down" });
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
        if (input.disabled) return;
        input.value = "";
        input.closest(".crossword-cell").classList.remove("correct", "incorrect");
      });
      if (status) status.textContent = "Grid cleared.";
      progressSounds?.sync();
      saveProgress();
    });
  }

  if (revealButton) {
    revealButton.addEventListener("click", revealSolution);
  }

  revealEntryButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const entry = entries.find((item) => (
        String(item.number) === button.dataset.entryNumber
        && item.direction === button.dataset.entryDirection
      ));
      if (entry) revealEntry(entry);
    });
  });

  checkEntryButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const entry = entries.find((item) => (
        String(item.number) === button.dataset.entryNumber
        && item.direction === button.dataset.entryDirection
      ));
      if (entry) checkEntry(entry);
    });
  });

  moreClueButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const list = button.nextElementSibling;
      if (!list || !list.classList.contains("clue-steps")) return;
      const hiddenSteps = Array.from(list.querySelectorAll("li[hidden]"));
      if (!hiddenSteps.length) return;
      list.hidden = false;
      hiddenSteps[0].hidden = false;
      const remaining = hiddenSteps.length - 1;
      button.textContent = remaining ? `More clues (${remaining})` : "No more clues";
      button.disabled = remaining === 0;
    });
  });
})();
