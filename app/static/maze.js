(function () {
  const root = document.querySelector(".maze-play");
  if (!root) return;

  const svg = root.querySelector(".maze-board");
  const trace = root.querySelector(".maze-trace-path");
  const progressUrl = root.dataset.progressUrl;
  const status = document.getElementById("maze-status");
  const completeButton = document.getElementById("maze-complete");
  const clearButton = document.getElementById("maze-clear");
  const width = Number(root.dataset.width || 0);
  const height = Number(root.dataset.height || 0);
  const cellSize = Number(root.dataset.cellSize || 24);
  let grid = [];
  let path = [];
  let drawing = false;

  try {
    grid = JSON.parse(root.dataset.grid || "[]");
  } catch (_error) {
    grid = [];
  }

  try {
    path = JSON.parse(root.dataset.path || "[]");
  } catch (_error) {
    path = [];
  }
  if (!Array.isArray(path) || path.length === 0) path = [{ x: 0, y: 0 }];

  function samePoint(a, b) {
    return Boolean(a && b && a.x === b.x && a.y === b.y);
  }

  function center(point) {
    return `${point.x * cellSize + cellSize / 2},${point.y * cellSize + cellSize / 2}`;
  }

  function isFinish(point) {
    return point.x === width - 1 && point.y === height - 1;
  }

  function setStatus(text) {
    if (status) status.textContent = text;
  }

  function isValidStep(from, to) {
    if (!from || !to) return false;
    if (to.x < 0 || to.y < 0 || to.x >= width || to.y >= height) return false;
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    let direction = "";
    if (dx === 1 && dy === 0) direction = "e";
    if (dx === -1 && dy === 0) direction = "w";
    if (dx === 0 && dy === 1) direction = "s";
    if (dx === 0 && dy === -1) direction = "n";
    if (!direction) return false;
    const cell = grid[from.y] && grid[from.y][from.x];
    return Boolean(cell && cell.walls && cell.walls[direction] === false);
  }

  function renderPath() {
    if (trace) trace.setAttribute("points", path.map(center).join(" "));
    const completed = isFinish(path[path.length - 1]);
    root.dataset.completed = completed ? "true" : "false";
    root.classList.toggle("complete", completed);
    if (completed) {
      setStatus("Solved. The traced path reached Finish.");
    } else if (path.length > 1) {
      setStatus(`${path.length} cells traced. Keep going to F.`);
    } else {
      setStatus("Drag from S to F. Backtrack over your path to undo.");
    }
  }

  async function savePath(forceCompleted) {
    const completed = forceCompleted || isFinish(path[path.length - 1]);
    root.dataset.completed = completed ? "true" : "false";
    root.classList.toggle("complete", completed);
    if (!progressUrl) return;
    try {
      await fetch(progressUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answers: {
            completed: completed ? "true" : "",
            path: JSON.stringify(path),
          },
        }),
      });
    } catch (_error) {
      setStatus("Progress could not be saved.");
    }
  }

  function cellFromEvent(event) {
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    const viewBox = svg.viewBox.baseVal;
    const scaleX = viewBox.width / rect.width;
    const scaleY = viewBox.height / rect.height;
    const x = Math.floor(((event.clientX - rect.left) * scaleX) / cellSize);
    const y = Math.floor(((event.clientY - rect.top) * scaleY) / cellSize);
    if (x < 0 || y < 0 || x >= width || y >= height) return null;
    return { x, y };
  }

  function visit(point) {
    if (!point) return;
    const last = path[path.length - 1];
    if (samePoint(point, last)) return;

    const existingIndex = path.findIndex((item) => samePoint(item, point));
    if (existingIndex >= 0) {
      path = path.slice(0, existingIndex + 1);
      renderPath();
      return;
    }

    if (!isValidStep(last, point)) {
      setStatus("That move crosses a wall.");
      return;
    }

    path.push(point);
    renderPath();
    if (isFinish(point)) savePath(true);
  }

  if (svg) {
    svg.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      svg.setPointerCapture(event.pointerId);
      drawing = true;
      const point = cellFromEvent(event);
      if (samePoint(point, { x: 0, y: 0 })) {
        path = [{ x: 0, y: 0 }];
        renderPath();
      } else {
        visit(point);
      }
    });

    svg.addEventListener("pointermove", (event) => {
      if (!drawing) return;
      event.preventDefault();
      visit(cellFromEvent(event));
    });

    svg.addEventListener("pointerup", (event) => {
      drawing = false;
      savePath(false);
      if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    });

    svg.addEventListener("pointercancel", () => {
      drawing = false;
      savePath(false);
    });
  }

  if (completeButton) {
    completeButton.addEventListener("click", () => {
      root.dataset.completed = "true";
      root.classList.add("complete");
      setStatus("Maze marked complete.");
      savePath(true);
    });
  }

  if (clearButton) {
    clearButton.addEventListener("click", () => {
      path = [{ x: 0, y: 0 }];
      renderPath();
      savePath(false);
    });
  }

  renderPath();
})();
