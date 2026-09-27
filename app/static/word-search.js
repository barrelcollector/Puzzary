(function () {
  const grid = document.querySelector(".word-grid.interactive");
  if (!grid) return;

  const answerBox = document.getElementById("answer-box");
  const wordList = document.getElementById("word-list");
  const words = new Set((grid.dataset.words || "").split(",").map((word) => word.trim().toUpperCase()).filter(Boolean));
  const found = new Set((answerBox.value || "").split(",").map((word) => word.trim().toUpperCase()).filter(Boolean));
  const cells = Array.from(grid.querySelectorAll("button[data-letter]"));
  const cellsByCoord = new Map(cells.map((cell) => [`${cell.dataset.x},${cell.dataset.y}`, cell]));
  let dragging = false;
  let activePointerId = null;
  let startCell = null;
  let lastCell = null;
  let selected = [];

  function syncAnswer() {
    answerBox.value = Array.from(found).sort().join(", ");
    wordList.querySelectorAll("[data-word]").forEach((item) => {
      item.classList.toggle("found", found.has(item.dataset.word.toUpperCase()));
    });
  }

  function clearSelected() {
    selected.forEach((cell) => cell.classList.remove("selected", "miss"));
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
    selected.forEach((cell) => cell.classList.remove("selected", "miss"));
    selected = lineCells(startCell, toCell);
    selected.forEach((cell) => cell.classList.add("selected"));
  }

  function selectedWord() {
    return selected.map((cell) => cell.dataset.letter).join("").toUpperCase();
  }

  function finishSelection() {
    if (!selected.length) return;
    const forward = selectedWord();
    const reverse = forward.split("").reverse().join("");
    const match = words.has(forward) ? forward : words.has(reverse) ? reverse : "";

    if (match) {
      found.add(match);
      selected.forEach((cell) => {
        cell.classList.remove("selected");
        cell.classList.add("found-cell");
      });
      selected = [];
      syncAnswer();
      return;
    }

    selected.forEach((cell) => {
      cell.classList.remove("selected");
      cell.classList.add("miss");
    });
    window.setTimeout(clearSelected, 350);
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

  syncAnswer();
})();
