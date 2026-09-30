(function () {
  const root = document.querySelector(".word-pyramid-play");
  if (!root) return;

  const inputs = Array.from(root.querySelectorAll("input[data-row]"));
  const status = document.getElementById("word-pyramid-status");
  const checkButton = document.getElementById("word-pyramid-check");
  const clearButton = document.getElementById("word-pyramid-clear");
  const revealButton = document.getElementById("reveal-solution");
  const rowRevealButtons = Array.from(root.querySelectorAll(".reveal-pyramid-row"));
  const clueButtons = Array.from(root.querySelectorAll(".more-pyramid-clue"));
  const progressUrl = root.dataset.progressUrl;
  let rows = [];
  let saveTimer = null;

  try {
    rows = JSON.parse(root.dataset.solution || "[]");
  } catch (_error) {
    rows = [];
  }

  function answerFor(index) {
    return String((rows[index] && rows[index].answer) || "").toUpperCase();
  }

  function currentAnswers() {
    const answers = {};
    inputs.forEach((input) => {
      const value = input.value.trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
      if (value) answers[input.dataset.row] = value;
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

  function setStatus(text) {
    if (status) status.textContent = text;
  }

  function cleanInput(input) {
    input.value = input.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, Number(input.maxLength || 20));
    input.closest(".word-pyramid-row").classList.remove("correct", "incorrect");
    queueSave();
  }

  function checkPyramid() {
    let correct = 0;
    inputs.forEach((input) => {
      const row = input.closest(".word-pyramid-row");
      const expected = answerFor(Number(input.dataset.row));
      const value = input.value.trim().toUpperCase();
      row.classList.remove("correct", "incorrect");
      if (!value) return;
      if (value === expected) {
        correct += 1;
        row.classList.add("correct");
      } else {
        row.classList.add("incorrect");
      }
    });
    setStatus(correct === inputs.length ? "Word pyramid complete." : `${correct} of ${inputs.length} rows correct.`);
  }

  function revealRow(index) {
    const input = inputs.find((item) => Number(item.dataset.row) === index);
    if (!input) return;
    input.value = answerFor(index);
    input.closest(".word-pyramid-row").classList.remove("incorrect");
    input.closest(".word-pyramid-row").classList.add("correct");
    setStatus(`Row ${index + 1} revealed.`);
    saveProgress();
  }

  function revealAll() {
    inputs.forEach((input) => {
      const index = Number(input.dataset.row);
      input.value = answerFor(index);
      input.closest(".word-pyramid-row").classList.remove("incorrect");
      input.closest(".word-pyramid-row").classList.add("correct");
    });
    setStatus("Solution revealed. Reset the puzzle to start over.");
    saveProgress();
  }

  inputs.forEach((input, index) => {
    input.addEventListener("input", () => {
      cleanInput(input);
      if (input.value.length >= Number(input.maxLength || 0) && inputs[index + 1]) {
        inputs[index + 1].focus();
      }
    });
  });

  if (checkButton) checkButton.addEventListener("click", checkPyramid);

  if (clearButton) {
    clearButton.addEventListener("click", () => {
      inputs.forEach((input) => {
        input.value = "";
        input.closest(".word-pyramid-row").classList.remove("correct", "incorrect");
      });
      setStatus("Pyramid cleared.");
      saveProgress();
    });
  }

  if (revealButton) revealButton.addEventListener("click", revealAll);

  rowRevealButtons.forEach((button) => {
    button.addEventListener("click", () => revealRow(Number(button.dataset.row)));
  });

  clueButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const list = button.nextElementSibling;
      if (!list || !list.classList.contains("pyramid-clue-steps")) return;
      const hiddenSteps = Array.from(list.querySelectorAll("small[hidden]"));
      if (!hiddenSteps.length) return;
      list.hidden = false;
      hiddenSteps[0].hidden = false;
      const remaining = hiddenSteps.length - 1;
      button.textContent = remaining ? `More clues (${remaining})` : "No more clues";
      button.disabled = remaining === 0;
    });
  });
})();
