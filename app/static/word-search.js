(function () {
  const grid = document.querySelector(".word-grid.interactive");
  if (!grid) return;

  const answerBox = document.getElementById("answer-box");
  const wordList = document.getElementById("word-list");
  const words = new Set((grid.dataset.words || "").split(",").map((word) => word.trim().toUpperCase()).filter(Boolean));
  const found = new Set((answerBox.value || "").split(",").map((word) => word.trim().toUpperCase()).filter(Boolean));
  let dragging = false;
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
    dragging = true;
    grid.setPointerCapture(event.pointerId);
    clearSelected();
    addCell(cell);
  });

  grid.addEventListener("pointerover", (event) => {
    if (!dragging) return;
    addCell(event.target.closest("button[data-letter]"));
  });

  grid.addEventListener("pointerup", () => {
    dragging = false;
    finishSelection();
  });

  grid.addEventListener("pointercancel", () => {
    dragging = false;
    clearSelected();
  });

  syncAnswer();
})();
