(function () {
  const grid = document.querySelector(".word-grid.interactive");
  if (!grid) return;

  const answerBox = document.getElementById("answer-box");
  const wordList = document.getElementById("word-list");
  const words = new Set((grid.dataset.words || "").split(",").map((word) => word.trim().toUpperCase()).filter(Boolean));
  const found = new Set((answerBox.value || "").split(",").map((word) => word.trim().toUpperCase()).filter(Boolean));
  let dragging = false;
  let activePointerId = null;
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
    if (!cell || selected.includes(cell) || cell.classList.contains("found-cell")) return;
    selected.push(cell);
    cell.classList.add("selected");
  }

  function cellFromPoint(event) {
    const target = document.elementFromPoint(event.clientX, event.clientY);
    return target ? target.closest(".word-grid.interactive button[data-letter]") : null;
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
    grid.setPointerCapture(event.pointerId);
    clearSelected();
    addCell(cell);
  });

  grid.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    event.preventDefault();
    addCell(cellFromPoint(event));
  });

  grid.addEventListener("pointerup", (event) => {
    if (activePointerId !== null && event.pointerId !== activePointerId) return;
    event.preventDefault();
    dragging = false;
    activePointerId = null;
    if (grid.hasPointerCapture(event.pointerId)) {
      grid.releasePointerCapture(event.pointerId);
    }
    finishSelection();
  });

  grid.addEventListener("pointercancel", (event) => {
    dragging = false;
    activePointerId = null;
    if (grid.hasPointerCapture(event.pointerId)) {
      grid.releasePointerCapture(event.pointerId);
    }
    clearSelected();
  });

  syncAnswer();
})();
