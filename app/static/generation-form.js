(function () {
  const schemaElement = document.getElementById("generation-schemas");
  const forms = Array.from(document.querySelectorAll("[data-generation-form]"));
  if (!schemaElement || forms.length === 0) return;

  let schemas = {};
  try {
    schemas = JSON.parse(schemaElement.textContent || "{}");
  } catch (_error) {
    schemas = {};
  }

  function setGroupVisible(group, visible) {
    group.classList.toggle("is-hidden", !visible);
    group.querySelectorAll("input, select, textarea").forEach((control) => {
      control.disabled = !visible;
      if (control.hasAttribute("data-required-when-visible")) {
        control.required = visible;
      }
    });
  }

  function fieldControl(field) {
    let control;
    if (field.field_type === "select") {
      control = document.createElement("select");
      (field.options || []).forEach((option) => {
        const optionElement = document.createElement("option");
        optionElement.value = option.value;
        optionElement.textContent = option.label;
        if (String(option.value) === String(field.default || "")) optionElement.selected = true;
        control.appendChild(optionElement);
      });
    } else {
      control = document.createElement("input");
      control.type = field.field_type === "number" ? "number" : "text";
      control.value = field.default || "";
      if (field.minimum !== null && field.minimum !== undefined) control.min = field.minimum;
      if (field.maximum !== null && field.maximum !== undefined) control.max = field.maximum;
    }
    control.name = `gen_${field.name}`;
    control.required = Boolean(field.required);
    control.dataset.summaryLabel = field.label;
    return control;
  }

  function renderFields(form, schema) {
    const target = form.querySelector("[data-generation-fields]");
    if (!target) return;
    target.innerHTML = "";
    const fields = schema.generation_fields || [];
    if (fields.length === 0) {
      const note = document.createElement("p");
      note.className = "muted";
      note.textContent = "This puzzle type has no extra setup options yet.";
      target.appendChild(note);
      return;
    }
    fields.forEach((field) => {
      const label = document.createElement("label");
      label.textContent = field.label;
      label.appendChild(fieldControl(field));
      if (field.help_text) {
        const help = document.createElement("small");
        help.className = "muted";
        help.textContent = field.help_text;
        label.appendChild(help);
      }
      target.appendChild(label);
    });
  }

  function selectedText(control) {
    if (!control) return "";
    if (control.tagName === "SELECT") {
      return control.selectedOptions.length ? control.selectedOptions[0].textContent.trim() : "";
    }
    return control.value.trim();
  }

  function checkedLabels(form, name) {
    return Array.from(form.querySelectorAll(`input[name="${name}"]:checked:not(:disabled)`))
      .map((input) => input.closest("label") ? input.closest("label").textContent.trim() : input.value);
  }

  function addSummary(summary, label, value) {
    if (!summary || !value) return;
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    summary.append(term, detail);
  }

  function renderSummary(form) {
    const summary = form.querySelector("[data-generation-summary]");
    if (!summary) return;
    summary.innerHTML = "";
    const typeSelect = form.querySelector("[data-puzzle-type]");
    const schema = schemas[typeSelect ? typeSelect.value : ""] || {};
    const needsWordBank = Boolean(schema.needs_word_bank);
    const usesTheme = !needsWordBank && schema.theme_mode !== "none";
    addSummary(summary, "Puzzle type", selectedText(typeSelect));
    if (needsWordBank) {
      addSummary(summary, "Word bank", selectedText(form.querySelector('[name="bank_id"], select[name="theme"]')));
    } else if (usesTheme) {
      addSummary(summary, "Puzzle theme", selectedText(form.querySelector('[name="theme"]:not(:disabled)')));
    } else {
      addSummary(summary, "Source", "Not needed");
    }
    addSummary(summary, "Difficulty", selectedText(form.querySelector('select[name="difficulty"]')));
    addSummary(summary, "Seed", selectedText(form.querySelector('input[name="seed"]')) || "Fresh random layout");
    form.querySelectorAll('[name^="gen_"]:not(:disabled)').forEach((control) => {
      addSummary(summary, control.dataset.summaryLabel || control.name.replace(/^gen_/, ""), selectedText(control) || "Default");
    });
    addSummary(summary, "Category", selectedText(form.querySelector('select[name="category_id"]')));
    addSummary(summary, "Tags", checkedLabels(form, "tag_ids").join(", "));
    addSummary(summary, "Excluded word tags", checkedLabels(form, "excluded_tag_ids").join(", "));
  }

  function validateStep(step) {
    const controls = Array.from(step.querySelectorAll("input, select, textarea")).filter((control) => !control.disabled);
    for (const control of controls) {
      if (!control.checkValidity()) {
        control.reportValidity();
        return false;
      }
    }
    return true;
  }

  function showStep(form, index) {
    const steps = Array.from(form.querySelectorAll("[data-wizard-step]"));
    const indicators = Array.from(form.querySelectorAll("[data-step-indicator]"));
    const safeIndex = Math.max(0, Math.min(index, steps.length - 1));
    steps.forEach((step, stepIndex) => step.classList.toggle("is-hidden", stepIndex !== safeIndex));
    indicators.forEach((indicator, stepIndex) => {
      indicator.classList.toggle("active", stepIndex === safeIndex);
      indicator.classList.toggle("complete", stepIndex < safeIndex);
    });
    form.dataset.currentStep = String(safeIndex);
    const backButton = form.querySelector("[data-wizard-back]");
    const nextButton = form.querySelector("[data-wizard-next]");
    const submitButton = form.querySelector("[data-wizard-submit]");
    if (backButton) backButton.disabled = safeIndex === 0;
    if (nextButton) nextButton.classList.toggle("is-hidden", safeIndex === steps.length - 1);
    if (submitButton) submitButton.classList.toggle("is-hidden", safeIndex !== steps.length - 1);
    if (safeIndex === steps.length - 1) renderSummary(form);
  }

  function updateForm(form) {
    const typeSelect = form.querySelector("[data-puzzle-type]");
    const schema = schemas[typeSelect ? typeSelect.value : ""] || {};
    const needsWordBank = Boolean(schema.needs_word_bank);
    const usesTheme = !needsWordBank && schema.theme_mode !== "none";
    form.querySelectorAll('[data-source-group="word_bank"]').forEach((group) => setGroupVisible(group, needsWordBank));
    form.querySelectorAll('[data-source-group="theme"]').forEach((group) => setGroupVisible(group, usesTheme));
    form.querySelectorAll('[data-source-group="none"]').forEach((group) => setGroupVisible(group, !needsWordBank && !usesTheme));
    renderFields(form, schema);
    renderSummary(form);
  }

  forms.forEach((form) => {
    form.querySelectorAll('[data-source-group="theme"] input').forEach((input) => {
      input.setAttribute("data-required-when-visible", "true");
    });
    updateForm(form);
    showStep(form, 0);

    const typeSelect = form.querySelector("[data-puzzle-type]");
    if (typeSelect) {
      typeSelect.addEventListener("change", () => {
        updateForm(form);
        showStep(form, Number(form.dataset.currentStep || 0));
      });
    }

    const nextButton = form.querySelector("[data-wizard-next]");
    if (nextButton) {
      nextButton.addEventListener("click", () => {
        const steps = Array.from(form.querySelectorAll("[data-wizard-step]"));
        const currentStep = Number(form.dataset.currentStep || 0);
        if (!validateStep(steps[currentStep])) return;
        showStep(form, currentStep + 1);
      });
    }

    const backButton = form.querySelector("[data-wizard-back]");
    if (backButton) {
      backButton.addEventListener("click", () => showStep(form, Number(form.dataset.currentStep || 0) - 1));
    }

    form.addEventListener("input", () => renderSummary(form));
    form.addEventListener("change", () => renderSummary(form));
  });
})();
