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

  function setSelectValue(item, value) {
    if (!item) return;
    item.value = String(value);
    item.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setInputValue(item, value) {
    if (!item) return;
    item.value = String(value);
    item.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function renderSudokuEditor(form, parts) {
    const solutionControl = control(form, "solution_grid");
    const givensControl = control(form, "givens");
    if (!solutionControl || !givensControl) return;
    hideSourceLabels(form, ["solution_grid", "givens"]);
    let size = Number(control(form, "grid_size") ? control(form, "grid_size").value : 9) || 9;
    if (!Number.isFinite(size) || size < 4 || size > 9) size = 9;
    let solution = parseSudokuSolution(solutionControl.value, size);
    let givens = parseGivenSet(givensControl.value);
    if (!givens.size) {
      givens = new Set(["0,0", "1,0", "0,1", "1,1"]);
    }

    const sizeSelect = document.createElement("select");
    [4, 6, 9].forEach((value) => {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = `${value}x${value}`;
      option.selected = value === size;
      sizeSelect.append(option);
    });
    const reset = button("Reset Pattern");
    const clearGivens = button("Clear Givens");
    parts.toolbar.append(document.createElement("h3"), labelWrap("Board size", sizeSelect), reset, clearGivens);
    parts.toolbar.querySelector("h3").textContent = "Sudoku Canvas";
    parts.properties.innerHTML = "<h3>Cell Tools</h3><p class=\"muted\">Type the solved value in a cell. Click the dot to mark whether that cell is a starting given.</p>";

    function sync() {
      solutionControl.value = solution.map((row) => row.join("")).join("\n");
      givensControl.value = Array.from(givens).map((key) => {
        const [x, y] = key.split(",").map(Number);
        return `${x},${y},${solution[y][x]}`;
      }).join("\n");
      setSelectValue(control(form, "grid_size"), size);
      if (size === 4) {
        setInputValue(control(form, "box_rows"), 2);
        setInputValue(control(form, "box_cols"), 2);
      } else if (size === 6) {
        setInputValue(control(form, "box_rows"), 2);
        setInputValue(control(form, "box_cols"), 3);
      } else {
        setInputValue(control(form, "box_rows"), 3);
        setInputValue(control(form, "box_cols"), 3);
      }
    }

    function draw() {
      parts.canvas.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "level-sudoku-grid";
      grid.style.setProperty("--builder-grid-size", String(size));
      solution.forEach((row, y) => {
        row.forEach((value, x) => {
          const wrap = document.createElement("label");
          wrap.className = "level-sudoku-cell";
          const input = document.createElement("input");
          input.inputMode = "numeric";
          input.maxLength = 1;
          input.value = String(value || "");
          input.addEventListener("input", () => {
            const next = Number(input.value.replace(/[^0-9]/g, "").slice(0, 1) || 0);
            solution[y][x] = Math.max(1, Math.min(size, next || 1));
            input.value = String(solution[y][x]);
            sync();
          });
          const toggle = document.createElement("button");
          toggle.type = "button";
          toggle.className = givens.has(`${x},${y}`) ? "level-cell-toggle active" : "level-cell-toggle";
          toggle.title = "Toggle given";
          toggle.textContent = "G";
          toggle.addEventListener("click", () => {
            const key = `${x},${y}`;
            if (givens.has(key)) givens.delete(key);
            else givens.add(key);
            sync();
            draw();
          });
          wrap.append(input, toggle);
          grid.append(wrap);
        });
      });
      parts.canvas.append(grid);
    }

    sizeSelect.addEventListener("change", () => {
      size = Number(sizeSelect.value);
      solution = solvedPattern(size);
      givens = new Set(["0,0", "1,0", "0,1", "1,1"]);
      sync();
      draw();
    });
    reset.addEventListener("click", () => {
      solution = solvedPattern(size);
      sync();
      draw();
    });
    clearGivens.addEventListener("click", () => {
      givens = new Set();
      sync();
      draw();
    });

    sync();
    draw();
  }

  function solvedPattern(size) {
    const boxCols = size === 6 ? 3 : Math.sqrt(size);
    const boxRows = size === 6 ? 2 : Math.sqrt(size);
    return Array.from({ length: size }, (_row, row) =>
      Array.from({ length: size }, (_col, col) => ((row * boxCols + Math.floor(row / boxRows) + col) % size) + 1)
    );
  }

  function parseSudokuSolution(value, size) {
    const rows = String(value || "").split(/\r?\n/).map((line) => Array.from(line).filter((char) => /[1-9]/.test(char)).map(Number)).filter((row) => row.length);
    if (rows.length === size && rows.every((row) => row.length === size)) return rows;
    return solvedPattern(size);
  }

  function parseGivenSet(value) {
    return new Set(String(value || "").split(/\r?\n/).map((line) => line.split(/[,:=]/).slice(0, 2).map((part) => part.trim()).join(",")).filter((key) => /^\d+,\d+$/.test(key)));
  }

  function renderCrossEquationsEditor(form, parts) {
    const solutionControl = control(form, "solution_grid");
    const rowOpsControl = control(form, "row_ops");
    const colOpsControl = control(form, "col_ops");
    const givensControl = control(form, "givens");
    if (!solutionControl || !rowOpsControl || !colOpsControl || !givensControl) return;
    hideSourceLabels(form, ["solution_grid", "row_ops", "col_ops", "givens"]);
    let size = Number(control(form, "grid_size") ? control(form, "grid_size").value : 3) || 3;
    if (size < 3 || size > 5) size = 3;
    let numbers = parseNumberGrid(solutionControl.value, size);
    let rowOps = parseOps(rowOpsControl.value, size);
    let colOps = parseOps(colOpsControl.value, size);
    let givens = parseGivenSet(givensControl.value);
    if (!givens.size) givens = new Set(["0,0", "2,0", "0,2"]);

    const sizeSelect = document.createElement("select");
    [3, 4, 5].forEach((value) => {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = `${value}x${value}`;
      option.selected = value === size;
      sizeSelect.append(option);
    });
    const makeAdd = button("All +");
    const clearGivens = button("Clear Starters");
    parts.toolbar.append(document.createElement("h3"), labelWrap("Number cells", sizeSelect), makeAdd, clearGivens);
    parts.toolbar.querySelector("h3").textContent = "Equation Canvas";
    parts.properties.innerHTML = "<h3>Equation Tools</h3><p class=\"muted\">Edit number cells, choose operators, and toggle starter cells with G.</p>";

    function sync() {
      solutionControl.value = numbers.map((row) => row.join(",")).join("\n");
      rowOpsControl.value = rowOps.map((row) => row.join(",")).join("\n");
      colOpsControl.value = colOps.map((row) => row.join(",")).join("\n");
      givensControl.value = Array.from(givens).map((key) => {
        const [rx, ry] = key.split(",").map(Number);
        return `${rx},${ry},${numbers[ry / 2][rx / 2]}`;
      }).join("\n");
      setSelectValue(control(form, "grid_size"), size);
    }

    function draw() {
      parts.canvas.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "level-equation-grid";
      grid.style.setProperty("--builder-grid-size", String(size * 2 - 1));
      for (let y = 0; y < size * 2 - 1; y += 1) {
        for (let x = 0; x < size * 2 - 1; x += 1) {
          if (x % 2 === 0 && y % 2 === 0) {
            const wrap = document.createElement("label");
            wrap.className = "level-equation-cell";
            const input = document.createElement("input");
            input.type = "number";
            input.value = String(numbers[y / 2][x / 2]);
            input.addEventListener("input", () => {
              numbers[y / 2][x / 2] = Number(input.value || 1);
              sync();
            });
            const toggle = document.createElement("button");
            toggle.type = "button";
            toggle.className = givens.has(`${x},${y}`) ? "level-cell-toggle active" : "level-cell-toggle";
            toggle.textContent = "G";
            toggle.addEventListener("click", () => {
              const key = `${x},${y}`;
              if (givens.has(key)) givens.delete(key);
              else givens.add(key);
              sync();
              draw();
            });
            wrap.append(input, toggle);
            grid.append(wrap);
          } else if (y % 2 === 0) {
            const operatorIndex = Math.floor(x / 2);
            grid.append(operatorIndex === size - 2 ? equalsCell() : operatorSelect(rowOps[y / 2], operatorIndex, sync));
          } else if (x % 2 === 0) {
            const operatorIndex = Math.floor(y / 2);
            grid.append(operatorIndex === size - 2 ? equalsCell() : operatorSelect(colOps[x / 2], operatorIndex, sync));
          } else {
            const spacer = document.createElement("span");
            spacer.className = "level-equation-spacer";
            grid.append(spacer);
          }
        }
      }
      parts.canvas.append(grid);
    }

    sizeSelect.addEventListener("change", () => {
      size = Number(sizeSelect.value);
      numbers = defaultEquationGrid(size);
      rowOps = parseOps("", size);
      colOps = parseOps("", size);
      givens = new Set(["0,0"]);
      sync();
      draw();
    });
    makeAdd.addEventListener("click", () => {
      rowOps = parseOps("", size);
      colOps = parseOps("", size);
      sync();
      draw();
    });
    clearGivens.addEventListener("click", () => {
      givens = new Set();
      sync();
      draw();
    });
    sync();
    draw();
  }

  function parseNumberGrid(value, size) {
    const rows = String(value || "").split(/\r?\n/).map((line) => line.split(/[\s,]+/).map((part) => Number(part)).filter((value) => Number.isFinite(value))).filter((row) => row.length);
    if (rows.length === size && rows.every((row) => row.length === size)) return rows;
    return defaultEquationGrid(size);
  }

  function defaultEquationGrid(size) {
    const grid = Array.from({ length: size }, () => Array.from({ length: size }, () => 1));
    for (let y = 0; y < size - 1; y += 1) grid[y][size - 1] = size - 1;
    for (let x = 0; x < size - 1; x += 1) grid[size - 1][x] = size - 1;
    grid[size - 1][size - 1] = (size - 1) * (size - 1);
    return grid;
  }

  function parseOps(value, size) {
    const operatorCount = Math.max(1, size - 2);
    const rows = String(value || "").split(/\r?\n/).map((line) => line.split(/[\s,]+/).map((part) => part.replace("*", "x")).filter(Boolean)).filter(Boolean);
    while (rows.length < size) rows.push(Array.from({ length: operatorCount }, () => "+"));
    return rows.slice(0, size).map((row) => {
      const next = row.slice(0, operatorCount).map((op) => ["+", "-", "x", "/"].includes(op) ? op : "+");
      while (next.length < operatorCount) next.push("+");
      return next;
    });
  }

  function equalsCell() {
    const item = document.createElement("span");
    item.className = "level-equation-equals";
    item.textContent = "=";
    return item;
  }

  function operatorSelect(row, index, sync) {
    const select = document.createElement("select");
    select.className = "level-operator-select";
    ["+", "-", "x", "/"].forEach((op) => {
      const option = document.createElement("option");
      option.value = op;
      option.textContent = op;
      option.selected = row[index] === op;
      select.append(option);
    });
    select.addEventListener("change", () => {
      row[index] = select.value;
      sync();
    });
    return select;
  }

  function renderMazeEditor(form, parts) {
    const wallsControl = control(form, "maze_walls");
    if (!wallsControl) return;
    hideSourceLabels(form, ["maze_walls"]);
    let maze = parseMaze(wallsControl.value, 10, 10);
    const widthInput = document.createElement("input");
    widthInput.type = "number";
    widthInput.min = "5";
    widthInput.max = "30";
    widthInput.value = String(maze.width);
    const heightInput = document.createElement("input");
    heightInput.type = "number";
    heightInput.min = "5";
    heightInput.max = "30";
    heightInput.value = String(maze.height);
    const resize = button("Resize");
    const carve = button("Open All");
    const box = button("Box All");
    parts.toolbar.append(document.createElement("h3"), labelWrap("Width", widthInput), labelWrap("Height", heightInput), resize, carve, box);
    parts.toolbar.querySelector("h3").textContent = "Maze Wall Canvas";
    parts.properties.innerHTML = "<h3>Wall Tools</h3><p class=\"muted\">Click a wall segment to open or close it. The start and finish openings are preserved.</p>";

    function sync() {
      wallsControl.value = JSON.stringify(maze);
      setSelectValue(control(form, "maze_size"), "custom");
      setInputValue(control(form, "custom_width"), maze.width);
      setInputValue(control(form, "custom_height"), maze.height);
    }

    function toggleWall(x, y, direction) {
      const cell = maze.cells[`${x},${y}`];
      cell[direction] = !cell[direction];
      const opposite = { n: "s", s: "n", e: "w", w: "e" }[direction];
      const dx = direction === "e" ? 1 : direction === "w" ? -1 : 0;
      const dy = direction === "s" ? 1 : direction === "n" ? -1 : 0;
      const neighbor = maze.cells[`${x + dx},${y + dy}`];
      if (neighbor) neighbor[opposite] = cell[direction];
      maze.cells["0,0"].w = false;
      maze.cells[`${maze.width - 1},${maze.height - 1}`].e = false;
      sync();
      draw();
    }

    function draw() {
      parts.canvas.innerHTML = "";
      const grid = document.createElement("div");
      grid.className = "level-maze-grid";
      grid.style.setProperty("--maze-width", String(maze.width));
      for (let y = 0; y < maze.height; y += 1) {
        for (let x = 0; x < maze.width; x += 1) {
          const cell = document.createElement("div");
          cell.className = "level-maze-cell";
          const walls = maze.cells[`${x},${y}`];
          ["n", "e", "s", "w"].forEach((direction) => {
            const wall = document.createElement("button");
            wall.type = "button";
            wall.className = `maze-wall maze-wall-${direction}${walls[direction] ? " closed" : ""}`;
            wall.title = `${direction.toUpperCase()} wall`;
            wall.addEventListener("click", () => toggleWall(x, y, direction));
            cell.append(wall);
          });
          if (x === 0 && y === 0) cell.dataset.marker = "S";
          if (x === maze.width - 1 && y === maze.height - 1) cell.dataset.marker = "F";
          grid.append(cell);
        }
      }
      parts.canvas.append(grid);
    }

    resize.addEventListener("click", () => {
      maze = parseMaze("", Number(widthInput.value || 10), Number(heightInput.value || 10));
      sync();
      draw();
    });
    carve.addEventListener("click", () => {
      Object.values(maze.cells).forEach((cell) => {
        cell.n = false; cell.e = false; cell.s = false; cell.w = false;
      });
      sync();
      draw();
    });
    box.addEventListener("click", () => {
      Object.values(maze.cells).forEach((cell) => {
        cell.n = true; cell.e = true; cell.s = true; cell.w = true;
      });
      sync();
      draw();
    });
    sync();
    draw();
  }

  function parseMaze(value, fallbackWidth, fallbackHeight) {
    try {
      const parsed = JSON.parse(String(value || ""));
      if (parsed && parsed.cells) return parsed;
    } catch (_error) {
      // Build a fresh grid below.
    }
    const width = Math.max(5, Math.min(30, fallbackWidth || 10));
    const height = Math.max(5, Math.min(30, fallbackHeight || width));
    const cells = {};
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        cells[`${x},${y}`] = { n: y === 0, e: x === width - 1, s: y === height - 1, w: x === 0 };
      }
    }
    cells["0,0"].w = false;
    cells[`${width - 1},${height - 1}`].e = false;
    return { width, height, cells };
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
    if (type === "sudoku") renderSudokuEditor(form, parts);
    if (type === "cross_equations") renderCrossEquationsEditor(form, parts);
    if (type === "maze") renderMazeEditor(form, parts);
  }

  forms.forEach((form) => {
    window.setTimeout(() => render(form), 0);
    const typeSelect = form.querySelector("[data-manual-puzzle-type]");
    if (typeSelect) {
      typeSelect.addEventListener("change", () => window.setTimeout(() => render(form), 0));
    }
  });
})();
