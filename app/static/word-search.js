(function () {
  const grid = document.querySelector(".word-grid.interactive");
  if (!grid) return;

  const wordList = document.getElementById("word-list");
  const status = document.getElementById("word-search-status");
  const checkButton = document.getElementById("word-search-check");
  const clearGuessesButton = document.getElementById("word-search-clear-guesses");
  const revealButton = document.getElementById("reveal-solution");
  const words = new Set((grid.dataset.words || "").split(",").map((word) => word.trim().toUpperCase()).filter(Boolean));
  const found = new Set((grid.dataset.foundWords || "").split(",").map((word) => word.trim().toUpperCase()).filter(Boolean));
  const progressUrl = grid.dataset.progressUrl;
  const autoConfirm = grid.dataset.autoConfirm !== "false";
  const cells = Array.from(grid.querySelectorAll("button[data-letter]"));
  const cellsByCoord = new Map(cells.map((cell) => [`${cell.dataset.x},${cell.dataset.y}`, cell]));
  let placements = [];
  let dragging = false;
  let activePointerId = null;
  let startCell = null;
  let lastCell = null;
  let selected = [];
  let guesses = [];
  let rejectedGuesses = [];
  let celebrationReady = false;
  let suppressCelebration = false;

  try {
    placements = JSON.parse(grid.dataset.placements || "[]");
  } catch (_error) {
    placements = [];
  }

  const placementsByWord = new Map(
    placements.map((placement) => [String(placement.word || "").toUpperCase(), placement])
  );

  function syncFoundWords() {
    wordList.querySelectorAll("[data-word]").forEach((item) => {
      item.classList.toggle("found", found.has(item.dataset.word.toUpperCase()));
    });
    if (status) {
      status.textContent = found.size === words.size
        ? "All words found."
        : `${found.size} of ${words.size} words found.`;
    }
    if (!suppressCelebration && celebrationReady && found.size === words.size && window.PuzzaryCelebrate) {
      window.PuzzaryCelebrate.complete();
    }
  }

  function confirmWord(word) {
    if (!word || !words.has(word)) return;
    found.add(word);
    markStoredWord(word);
    syncFoundWords();
    saveProgress();
  }

  function saveProgress() {
    if (!progressUrl) return;
    window.fetch(progressUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ found_words: Array.from(found).sort() }),
    }).catch(() => {
      if (status) status.textContent = "Progress could not be saved.";
    });
  }

  function cellsForPlacement(placement) {
    if (!placement || !placement.start || !placement.direction || !placement.word) return [];
    const [x, y] = placement.start;
    const [dx, dy] = placement.direction;
    const line = [];
    for (let i = 0; i < placement.word.length; i += 1) {
      const cell = cellsByCoord.get(`${x + dx * i},${y + dy * i}`);
      if (cell) line.push(cell);
    }
    return line;
  }

  function coordsForPlacement(placement) {
    return cellsForPlacement(placement).map((cell) => `${cell.dataset.x},${cell.dataset.y}`);
  }

  function sameCoordSet(first, second) {
    if (first.length !== second.length) return false;
    const firstSet = new Set(first);
    if (firstSet.size !== second.length) return false;
    return second.every((coord) => firstSet.has(coord));
  }

  function markStoredWord(word) {
    const placement = placementsByWord.get(word);
    cellsForPlacement(placement).forEach((cell) => cell.classList.add("found-cell"));
  }

  function revealSolution() {
    words.forEach((word) => {
      found.add(word);
      markStoredWord(word);
    });
    suppressCelebration = true;
    syncFoundWords();
    suppressCelebration = false;
    if (status) status.textContent = "Solution revealed. Reset the puzzle to start over.";
  }

  function clearSelected() {
    selected.forEach((cell) => cell.classList.remove("selected"));
    selected = [];
  }

  function addCell(cell) {
    if (!cell || selected.includes(cell)) return;
    selected.push(cell);
    cell.classList.add("selected");
  }

  function cellFromPoint(event) {
    const target = document.elementFromPoint(event.clientX, event.clientY);
    return target ? target.closest(".word-grid.interactive button[data-letter]") : null;
  }

  function lineCells(fromCell, toCell) {
    if (!fromCell || !toCell) return [];
    const x1 = Number(fromCell.dataset.x);
    const y1 = Number(fromCell.dataset.y);
    const x2 = Number(toCell.dataset.x);
    const y2 = Number(toCell.dataset.y);
    const dx = Math.sign(x2 - x1);
    const dy = Math.sign(y2 - y1);
    const xDistance = Math.abs(x2 - x1);
    const yDistance = Math.abs(y2 - y1);

    if (!(xDistance === 0 || yDistance === 0 || xDistance === yDistance)) {
      return [fromCell, toCell];
    }

    const length = Math.max(xDistance, yDistance) + 1;
    const line = [];
    for (let i = 0; i < length; i += 1) {
      const cell = cellsByCoord.get(`${x1 + dx * i},${y1 + dy * i}`);
      if (cell) line.push(cell);
    }
    return line;
  }

  function previewSelection(toCell) {
    selected.forEach((cell) => cell.classList.remove("selected"));
    selected = lineCells(startCell, toCell);
    selected.forEach((cell) => cell.classList.add("selected"));
  }

  function selectedWord() {
    return selected.map((cell) => cell.dataset.letter).join("").toUpperCase();
  }

  function selectionKey(cellsToKey) {
    return cellsToKey.map((cell) => `${cell.dataset.x},${cell.dataset.y}`).join("|");
  }

  function clearGuesses() {
    guesses = [];
    rejectedGuesses = [];
    clearSelected();
    cells.forEach((cell) => cell.classList.remove("candidate-cell", "miss"));
    syncFoundWords();
  }

  function addGuess() {
    if (!selected.length) return;
    const key = selectionKey(selected);
    const existing = guesses.find((guess) => guess.key === key);
    if (existing) {
      existing.cells.forEach((cell) => cell.classList.remove("candidate-cell", "miss"));
      guesses = guesses.filter((guess) => guess.key !== key);
      clearSelected();
      if (status) status.textContent = "Selection removed.";
      return;
    }
    const rejected = rejectedGuesses.find((guess) => guess.key === key);
    if (rejected) {
      rejected.cells.forEach((cell) => cell.classList.remove("selected", "miss"));
      rejectedGuesses = rejectedGuesses.filter((guess) => guess.key !== key);
      clearSelected();
      if (status) status.textContent = "Incorrect selection removed.";
      return;
    }
    const forward = selectedWord();
    const reverse = forward.split("").reverse().join("");
    const guess = {
      key,
      forward,
      reverse,
      cells: selected.slice(),
    };
    guesses.push(guess);
    guess.cells.forEach((cell) => {
      cell.classList.remove("selected", "miss");
      cell.classList.add("candidate-cell");
    });
    selected = [];
    if (status) status.textContent = `${guesses.length} selection${guesses.length === 1 ? "" : "s"} ready to check.`;
  }

  function checkGuesses() {
    if (!guesses.length) {
      if (status) status.textContent = "Make one or more selections first.";
      return;
    }
    let correct = 0;
    const matchedGuessKeys = new Set();
    const newlyCompletedWords = [];
    const singleSelectionWords = new Set();
    const incompleteWords = [];

    guesses.forEach((guess) => {
      const match = words.has(guess.forward) ? guess.forward : words.has(guess.reverse) ? guess.reverse : "";
      if (match) {
        found.add(match);
        newlyCompletedWords.push(match);
        singleSelectionWords.add(match);
        matchedGuessKeys.add(guess.key);
        correct += 1;
      }
    });

    words.forEach((word) => {
      if (found.has(word)) return;
      const placementCoords = coordsForPlacement(placementsByWord.get(word));
      if (!placementCoords.length) return;
      const coveringGuesses = guesses.filter((guess) => (
        guess.cells.some((cell) => placementCoords.includes(`${cell.dataset.x},${cell.dataset.y}`))
      ));
      if (!coveringGuesses.length) return;
      const guessCoords = coveringGuesses.flatMap((guess) => (
        guess.cells.map((cell) => `${cell.dataset.x},${cell.dataset.y}`)
      ));
      if (sameCoordSet(guessCoords, placementCoords)) {
        found.add(word);
        newlyCompletedWords.push(word);
        correct += 1;
        coveringGuesses.forEach((guess) => matchedGuessKeys.add(guess.key));
      } else if (guessCoords.some((coord) => placementCoords.includes(coord))) {
        incompleteWords.push(word);
      }
    });

    guesses.forEach((guess) => {
      guess.cells.forEach((cell) => cell.classList.remove("candidate-cell", "selected", "miss"));
      if (matchedGuessKeys.has(guess.key)) {
        guess.cells.forEach((cell) => cell.classList.add("found-cell"));
      } else {
        guess.cells.forEach((cell) => {
          if (!cell.classList.contains("found-cell")) cell.classList.add("miss");
        });
      }
    });
    newlyCompletedWords.forEach(markStoredWord);
    const wrongCount = guesses.filter((guess) => !matchedGuessKeys.has(guess.key)).length;
    rejectedGuesses = guesses.filter((guess) => !matchedGuessKeys.has(guess.key));
    guesses = [];
    syncFoundWords();
    saveProgress();
    if (status) {
      const remaining = Array.from(words).filter((word) => !found.has(word));
      const parts = [];
      if (correct) {
        parts.push(`${correct} word${correct === 1 ? "" : "s"} completed`);
        const multipart = newlyCompletedWords.filter((word) => !singleSelectionWords.has(word));
        if (multipart.length) parts.push(`${multipart.join(", ")} completed from multiple selections`);
      }
      if (wrongCount) parts.push(`${wrongCount} selection${wrongCount === 1 ? "" : "s"} did not complete a word`);
      if (incompleteWords.length) parts.push(`Incomplete: ${Array.from(new Set(incompleteWords)).join(", ")}`);
      parts.push(remaining.length ? `Remaining: ${remaining.join(", ")}` : "All words found");
      status.textContent = `${parts.join(". ")}.`;
    }
  }

  function finishSelection() {
    if (!selected.length) return;
    if (!autoConfirm) {
      addGuess();
      return;
    }
    const forward = selectedWord();
    const reverse = forward.split("").reverse().join("");
    const match = words.has(forward) ? forward : words.has(reverse) ? reverse : "";

    if (match) {
      selected.forEach((cell) => {
        cell.classList.remove("selected");
        cell.classList.add("found-cell");
      });
      selected = [];
      confirmWord(match);
      return;
    }

    selected.forEach((cell) => {
      cell.classList.remove("selected");
      cell.classList.add("miss");
    });
    const missed = selected.slice();
    selected = [];
    window.setTimeout(() => {
      missed.forEach((cell) => cell.classList.remove("miss"));
    }, 350);
  }

  grid.addEventListener("pointerdown", (event) => {
    const cell = event.target.closest("button[data-letter]");
    if (!cell) return;
    event.preventDefault();
    dragging = true;
    activePointerId = event.pointerId;
    startCell = cell;
    lastCell = cell;
    grid.setPointerCapture(event.pointerId);
    clearSelected();
    addCell(cell);
  });

  grid.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    event.preventDefault();
    const cell = cellFromPoint(event);
    if (!cell || cell === lastCell) return;
    lastCell = cell;
    previewSelection(cell);
  });

  grid.addEventListener("pointerup", (event) => {
    if (activePointerId !== null && event.pointerId !== activePointerId) return;
    event.preventDefault();
    dragging = false;
    activePointerId = null;
    if (grid.hasPointerCapture(event.pointerId)) {
      grid.releasePointerCapture(event.pointerId);
    }
    if (lastCell) {
      previewSelection(lastCell);
    }
    startCell = null;
    lastCell = null;
    finishSelection();
  });

  grid.addEventListener("pointercancel", (event) => {
    dragging = false;
    activePointerId = null;
    startCell = null;
    lastCell = null;
    if (grid.hasPointerCapture(event.pointerId)) {
      grid.releasePointerCapture(event.pointerId);
    }
    clearSelected();
  });

  found.forEach(markStoredWord);
  syncFoundWords();
  celebrationReady = true;

  if (checkButton) checkButton.addEventListener("click", checkGuesses);
  if (clearGuessesButton) clearGuessesButton.addEventListener("click", clearGuesses);

  if (revealButton) {
    revealButton.addEventListener("click", revealSolution);
  }
})();
