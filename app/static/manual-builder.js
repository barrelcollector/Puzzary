(function () {
  const forms = Array.from(document.querySelectorAll("[data-manual-form]"));
  if (!forms.length) return;

  const editorTypes = new Set(["word_search", "crossword", "word_pyramid", "sudoku", "cross_equations", "maze"]);

  function control(form, name) {
    return form.querySelector(`[name="manual_${name}"]`);
  }

  function controlLabel(form, name) {
    const item = control(form, name);
    return item ? item.closest("label") : null;
  }

  function hideSourceLabels(form, names) {
    form.querySelectorAll(".builder-source-hidden").forEach((item) => item.classList.remove("builder-source-hidden"));
    names.forEach((name) => {
      const label = controlLabel(form, name);
      if (label) label.classList.add("builder-source-hidden");
    });
  }

  function parsePipeLines(value) {
    return String(value || "").split(/\r?\n/).map((line) => {
      const parts = line.split("|").map((part) => part.trim());
      return {
        answer: (parts.shift() || "").toUpperCase().replace(/[^A-Z0-9]/g, ""),
        clues: parts.filter(Boolean),
      };
    }).filter((item) => item.answer);
  }

  function pipeLines(items) {
    return items.map((item) => [item.answer, ...(item.clues || [])].filter(Boolean).join(" | ")).join("\n");
  }

  function parseGrid(value) {
    return String(value || "").split(/\r?\n/).map((line) => line.toUpperCase().replace(/[^A-Z0-9. ]/g, "").replace(/[. ]/g, "")).filter(Boolean);
  }

  function emptyGrid(size) {
    return Array.from({ length: size }, () => "".padEnd(size, "."));
  }

  function normalizeGrid(rows, fallbackSize) {
    const size = Math.max(2, Math.min(30, fallbackSize || rows.length || 10));
    const normalized = rows.length ? rows : emptyGrid(size);
    const width = Math.max(size, ...normalized.map((row) => row.length));
    return normalized.map((row) => row.padEnd(width, ".").slice(0, width));
  }

  function gridText(rows) {
    return rows.map((row) => row.replace(/\./g, "")).join("\n");
  }

  function wordsFromText(value) {
    return String(value || "").split(/[\n,;]+/).map((word) => word.trim().toUpperCase().replace(/[^A-Z0-9]/g, "")).filter(Boolean);
  }

  function unique(items) {
    return Array.from(new Set(items.filter(Boolean)));
  }

  function button(label, className = "ghost-button") {
    const item = document.createElement("button");
    item.type = "button";
    item.className = className;
    item.textContent = label;
    return item;
  }

  function createShell(form, type) {
    const existing = form.querySelector("[data-level-editor]");
    if (existing) existing.remove();
    const target = form.querySelector("[data-manual-fields]");
    if (!target || !editorTypes.has(type)) return null;

    const shell = document.createElement("section");
    shell.className = "level-editor";
    shell.dataset.levelEditor = type;
    shell.innerHTML = `
      <aside class="level-toolbar" data-level-toolbar></aside>
      <div class="level-canvas" data-level-canvas></div>
      <aside class="level-properties" data-level-properties></aside>
    `;
    target.prepend(shell);
    return {
      shell,
      toolbar: shell.querySelector("[data-level-toolbar]"),
      canvas: shell.querySelector("[data-level-canvas]"),
      properties: shell.querySelector("[data-level-properties]"),
    };
  }

  function renderWordSearch(form, parts) {
    const gridControl = control(form, "grid");
    const wordsControl = control(form, "words");
    if (!gridControl || !wordsControl) return;
    hideSourceLabels(form, ["grid", "words"]);

    let rows = normalizeGrid(parseGrid(gridControl.value), 10);
    let words = unique(wordsFromText(wordsControl.value));

    const title = document.createElement("h3");
    title.textContent = "Letter Grid";
    const sizeInput = document.createElement("input");
    sizeInput.type = "number";
    sizeInput.min = "2";
    sizeInput.max = "30";
    sizeInput.value = String(rows.length);
    const resize = button("Resize");
    const fill = button("Fill Empty");
    const clear = button("Clear Grid");
    parts.toolbar.append(title, labelWrap("Grid size", sizeInput), resize, fill, clear);

    const wordTitle = document.createElement("h3");
    wordTitle.textContent = "Word Tray";
    const wordInput = document.createElement("input");
    wordInput.placeholder = "Add word";
    const addWord = button("Add Word");
    parts.properties.append(wordTitle, wordInput, addWord);

    function sync() {
      gridControl.value = rows.map((row) => row.replace(/\./g, "")).join("\n");
      wordsControl.value = words.join("\n");
    }

    function drawWords() {
      parts.properties.querySelectorAll("[data-word-chip]").forEach((item) => item.remove());
      words.forEach((word, index) => {
        const chip = document.createElement("span");
        chip.className = "level-chip";
        chip.dataset.wordChip = word;
        chip.draggable = true;
        chip.innerHTML = `<strong>${word}</strong>`;
        const remove = button("x", "icon-button");
        remove.title = `Remove ${word}`;
        remove.addEventListener("click", () => {
          words.splice(index, 1);
          sync();
          drawWords();
        });
        chip.append(remove);
        chip.addEventListener("dragstart", (event) => event.dataTransfer.setData("text/plain", word));
        parts.properties.append(chip);
      });
    }

    function drawGrid() {
      parts.canvas.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "level-word-grid";
      grid.style.setProperty("--builder-grid-size", String(rows[0].length || 1));
      rows.forEach((row, y) => {
        Array.from(row).forEach((letter, x) => {
          const cell = document.createElement("input");
          cell.maxLength = 1;
          cell.value = letter === "." ? "" : letter;
          cell.title = `Row ${y + 1}, column ${x + 1}`;
          cell.addEventListener("input", () => {
            const next = cell.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 1) || ".";
            rows[y] = rows[y].slice(0, x) + next + rows[y].slice(x + 1);
            cell.value = next === "." ? "" : next;
            sync();
          });
          cell.addEventListener("dragover", (event) => event.preventDefault());
          cell.addEventListener("drop", (event) => {
            event.preventDefault();
            const word = event.dataTransfer.getData("text/plain").toUpperCase();
            if (!word) return;
            let nextRow = rows[y].split("");
            for (let index = 0; index < word.length && x + index < nextRow.length; index += 1) {
              nextRow[x + index] = word[index];
            }
            rows[y] = nextRow.join("");
            sync();
            drawGrid();
          });
          grid.append(cell);
        });
      });
      parts.canvas.append(grid);
    }

    resize.addEventListener("click", () => {
      rows = normalizeGrid([], Number(sizeInput.value || 10));
      sync();
      drawGrid();
    });
    fill.addEventListener("click", () => {
      rows = rows.map((row) => Array.from(row).map((letter) => letter === "." ? String.fromCharCode(65 + Math.floor(Math.random() * 26)) : letter).join(""));
      sync();
      drawGrid();
    });
    clear.addEventListener("click", () => {
      rows = normalizeGrid([], Number(sizeInput.value || rows.length || 10));
      sync();
      drawGrid();
    });
    addWord.addEventListener("click", () => {
      const word = wordInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
      if (!word) return;
      words = unique([...words, word]);
      wordInput.value = "";
      sync();
      drawWords();
    });

    sync();
    drawGrid();
    drawWords();
  }

  function labelWrap(text, child) {
    const label = document.createElement("label");
    label.textContent = text;
    label.append(child);
    return label;
  }

  function renderEntryCards(form, parts, sourceName, titleText, addText) {
    const source = control(form, sourceName);
    if (!source) return;
    hideSourceLabels(form, [sourceName]);
    let items = parsePipeLines(source.value);
    if (!items.length) items = [{ answer: "", clues: [""] }];

    parts.toolbar.innerHTML = `<h3>${titleText}</h3><p class="muted">Drag cards to reorder. Each card syncs back to the module builder.</p>`;
    const add = button(addText);
    parts.toolbar.append(add);
    parts.properties.innerHTML = "<h3>Selected Card</h3><p class=\"muted\">Edit answers and clues directly on each card.</p>";

    function sync() {
      source.value = pipeLines(items);
    }

    function draw() {
      parts.canvas.innerHTML = "";
      const list = document.createElement("div");
      list.className = "level-card-list";
      items.forEach((item, index) => {
        const card = document.createElement("article");
        card.className = "level-card";
        card.draggable = true;
        card.dataset.index = String(index);
        const handle = document.createElement("span");
        handle.className = "level-handle";
        handle.textContent = String(index + 1);
        const answer = document.createElement("input");
        answer.value = item.answer;
        answer.placeholder = "ANSWER";
        answer.addEventListener("input", () => {
          item.answer = answer.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
          answer.value = item.answer;
          sync();
        });
        const clues = document.createElement("textarea");
        clues.rows = 3;
        clues.value = (item.clues || []).join("\n");
        clues.placeholder = "Primary clue\nExtra clue\nExtra clue";
        clues.addEventListener("input", () => {
          item.clues = clues.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
          sync();
        });
        const remove = button("Delete");
        remove.addEventListener("click", () => {
          items.splice(index, 1);
          sync();
          draw();
        });
        card.addEventListener("dragstart", (event) => event.dataTransfer.setData("text/plain", String(index)));
        card.addEventListener("dragover", (event) => event.preventDefault());
        card.addEventListener("drop", (event) => {
          event.preventDefault();
          const from = Number(event.dataTransfer.getData("text/plain"));
          const to = index;
          if (Number.isNaN(from) || from === to) return;
          const [moved] = items.splice(from, 1);
          items.splice(to, 0, moved);
          sync();
          draw();
        });
        card.append(handle, labelWrap("Answer", answer), labelWrap("Clues", clues), remove);
        list.append(card);
      });
      parts.canvas.append(list);
    }

    add.addEventListener("click", () => {
      items.push({ answer: "", clues: [""] });
      sync();
      draw();
    });
    sync();
    draw();
  }

  function renderControlBoard(form, parts, heading) {
    hideSourceLabels(form, []);
    parts.toolbar.innerHTML = `<h3>${heading}</h3><p class="muted">This module is using a generated board builder right now. Cell-by-cell editing needs a module-specific canvas next.</p>`;
    const controls = Array.from(form.querySelectorAll("[data-manual-fields] > label"));
    const board = document.createElement("div");
    board.className = "level-control-board";
    controls.forEach((item) => {
      const clone = item.cloneNode(true);
      clone.querySelectorAll("input, select, textarea").forEach((clonedControl) => {
        clonedControl.disabled = true;
      });
      board.append(clone);
    });
    parts.canvas.innerHTML = "";
    parts.canvas.append(board);
    parts.properties.innerHTML = "<h3>Next Canvas Layer</h3><p class=\"muted\">The level-editor shell is in place; this module still needs exact cell/wall editing tools.</p>";
  }

  function render(form) {
    const typeSelect = form.querySelector("[data-manual-puzzle-type]");
    if (!typeSelect) return;
    const type = typeSelect.value;
    const parts = createShell(form, type);
    if (!parts) return;
    if (type === "word_search") renderWordSearch(form, parts);
    if (type === "crossword") renderEntryCards(form, parts, "entries", "Crossword Entries", "Add Entry");
    if (type === "word_pyramid") renderEntryCards(form, parts, "rows", "Pyramid Rows", "Add Row");
    if (type === "sudoku") renderControlBoard(form, parts, "Sudoku Board Builder");
    if (type === "cross_equations") renderControlBoard(form, parts, "Cross Equations Builder");
    if (type === "maze") renderControlBoard(form, parts, "Maze Builder");
  }

  forms.forEach((form) => {
    window.setTimeout(() => render(form), 0);
    const typeSelect = form.querySelector("[data-manual-puzzle-type]");
    if (typeSelect) {
      typeSelect.addEventListener("change", () => window.setTimeout(() => render(form), 0));
    }
  });
})();
