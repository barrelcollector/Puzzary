(function () {
  const schemaElement = document.getElementById("generation-schemas");
  const forms = Array.from(document.querySelectorAll("[data-generation-form]"));
  const manualForms = Array.from(document.querySelectorAll("[data-manual-form]"));
  if (!schemaElement || (forms.length === 0 && manualForms.length === 0)) return;

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
    } else if (field.field_type === "checkboxes") {
      control = document.createElement("span");
      control.className = "check-list";
      const defaultValues = Array.isArray(field.default)
        ? field.default.map(String)
        : String(field.default || "").split(",").map((value) => value.trim()).filter(Boolean);
      (field.options || []).forEach((option) => {
        const optionLabel = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = `gen_${field.name}`;
        input.value = option.value;
        input.checked = defaultValues.includes(String(option.value));
        input.dataset.summaryLabel = field.label;
        optionLabel.append(input, option.label);
        control.appendChild(optionLabel);
      });
    } else if (field.field_type === "textarea") {
      control = document.createElement("textarea");
      control.rows = 8;
      control.value = field.default || "";
    } else {
      control = document.createElement("input");
      control.type = field.field_type === "number" ? "number" : "text";
      control.value = field.default || "";
      if (field.minimum !== null && field.minimum !== undefined) control.min = field.minimum;
      if (field.maximum !== null && field.maximum !== undefined) control.max = field.maximum;
    }
    if (field.field_type !== "checkboxes") {
      control.name = `gen_${field.name}`;
      control.required = Boolean(field.required);
      control.dataset.summaryLabel = field.label;
    }
    return control;
  }

  function setFieldPrefix(control, field, prefix) {
    if (field.field_type === "checkboxes") {
      control.querySelectorAll("input").forEach((input) => {
        input.name = `${prefix}${field.name}`;
      });
      return control;
    }
    control.name = `${prefix}${field.name}`;
    return control;
  }

  function applyFieldConditions(form) {
    const manual = form.hasAttribute('data-manual-form');
    const prefix = manual ? 'manual_' : 'gen_';
    const selector = manual ? '[data-manual-puzzle-type]' : '[data-puzzle-type]';
    const schema = schemas[form.querySelector(selector)?.value] || {};
    const fields = manual ? schema.manual_editor_fields : schema.generation_fields;
    const active = !manual || form.querySelector('input[data-manual-mode]:checked')?.value === 'helper';
    (fields || []).forEach(field => {
      if (!Object.keys(field.enabled_when || {}).length) return;
      const control = form.querySelector(`[name="${prefix}${field.name}"]`);
      if (control) control.disabled = !active || !Object.entries(field.enabled_when).every(([name, value]) => form.querySelector(`[name="${prefix}${name}"]`)?.value === value);
    });
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
    applyFieldConditions(form);
    renderSudokuProgressivePreview(form);
  }

  function selectedText(control) {
    if (!control) return "";
    if (control.tagName === "SELECT") {
      return Array.from(control.selectedOptions).map((option) => option.textContent.trim()).join(", ");
    }
    if (control.type === "checkbox") {
      return control.checked ? (control.closest("label") ? control.closest("label").textContent.trim() : control.value) : "";
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

  function renderSudokuProgressivePreview(form, manual = false) {
    const type = form.querySelector(manual ? '[data-manual-puzzle-type]' : '[data-puzzle-type]')?.value;
    const target = form.querySelector(manual ? '[data-manual-fields]' : '[data-generation-fields]');
    if (!target || type !== 'sudoku') return;
    const prefix = manual ? 'manual_' : 'gen_';
    let preview = target.querySelector('[data-sudoku-progressive-preview]');
    if (!preview) {
      preview = document.createElement('div');
      preview.className = 'notice'; preview.dataset.sudokuProgressivePreview = '';
      target.append(preview);
    }
    const enabled = form.querySelector(`[name="${prefix}progressive_solve"]`)?.value === 'yes';
    if (!enabled) {
      preview.innerHTML = '<strong>Progressive solve preview:</strong> Off. Players complete the full grid normally.';
      return;
    }
    const units = Array.from(form.querySelectorAll(`[name="${prefix}progressive_units"]:checked:not(:disabled)`))
      .map(input => input.closest('label')?.textContent.trim() || input.value);
    const validation = selectedText(form.querySelector(`[name="${prefix}progressive_validation"]`));
    const order = selectedText(form.querySelector(`[name="${prefix}progressive_order"]`));
    const marked = form.querySelector(`[name="${prefix}progressive_mark"]`)?.value === 'yes';
    const locked = form.querySelector(`[name="${prefix}progressive_lock"]`)?.value === 'yes';
    const sequence = selectedText(form.querySelector(`[name="${prefix}progressive_sequence"]`));
    preview.replaceChildren();
    const heading = document.createElement('strong'); heading.textContent = 'Progressive solve preview';
    const description = document.createElement('p'); description.className = 'muted';
    description.textContent = `${validation || 'Automatic validation'}; ${order || 'Any order'}${sequence ? ` (${sequence})` : ''}. Completed cells are ${marked ? 'marked' : 'not marked'} and ${locked ? 'locked' : 'still editable'}.`;
    const chips = document.createElement('div'); chips.className = 'sudoku-unit-list';
    (units.length ? units : ['Rows']).forEach((label,index) => {
      const chip = document.createElement('span'); chip.className = `sudoku-unit${index === 0 ? ' active' : ''}`;
      chip.textContent = `${label} — ${index === 0 ? 'active' : 'incomplete'}`; chips.append(chip);
    });
    preview.append(heading,description,chips);
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
      addSummary(summary, "Word banks", selectedText(form.querySelector('[name="bank_ids"], [name="word_bank_slugs"], select[name="theme"]')));
    } else if (usesTheme) {
      addSummary(summary, "Puzzle theme", selectedText(form.querySelector('[name="theme"]:not(:disabled)')));
    } else {
      addSummary(summary, "Source", "Not needed");
    }
    addSummary(summary, "Difficulty", selectedText(form.querySelector('select[name="difficulty"]')));
    addSummary(summary, "Seed", selectedText(form.querySelector('input[name="seed"]')) || "Fresh random layout");
    addSummary(summary, "Completion message", selectedText(form.querySelector('[name="completion_message"]')) || "Default message");
    const summarizedCheckboxes = new Set();
    form.querySelectorAll('[name^="gen_"]:not(:disabled)').forEach((control) => {
      if (control.type === "checkbox") {
        if (summarizedCheckboxes.has(control.name)) return;
        summarizedCheckboxes.add(control.name);
        const checked = Array.from(form.querySelectorAll(`[name="${control.name}"]:checked:not(:disabled)`))
          .map((input) => input.closest("label") ? input.closest("label").textContent.trim() : input.value);
        addSummary(summary, control.dataset.summaryLabel || control.name.replace(/^gen_/, ""), checked.join(", ") || "Difficulty default");
        return;
      }
      addSummary(summary, control.dataset.summaryLabel || control.name.replace(/^gen_/, ""), selectedText(control) || "Default");
    });
    addSummary(summary, "Category", selectedText(form.querySelector('select[name="category_id"]')));
    addSummary(summary, "Tags", checkedLabels(form, "tag_ids").join(", "));
    addSummary(summary, "Excluded word tags", checkedLabels(form, "excluded_tag_ids").join(", "));
    addSummary(summary, "Auto-archive", selectedText(form.querySelector('input[name="auto_archive_at"]')) || "No automatic archive");
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

    form.addEventListener("input", () => { renderSummary(form); renderSudokuProgressivePreview(form); });
    form.addEventListener("change", () => { applyFieldConditions(form); renderSummary(form); renderSudokuProgressivePreview(form); });
  });

  function manualEditorValues() {
    const valuesElement = document.getElementById("manual-editor-values");
    if (!valuesElement) return {};
    try {
      return JSON.parse(valuesElement.textContent || "{}");
    } catch (_error) {
      return {};
    }
  }

  function setControlValue(control, field, value) {
    if (value === undefined || value === null || value === "") return;
    if (field.field_type === "checkboxes") {
      const values = Array.isArray(value) ? value.map(String) : String(value).split(",").map((item) => item.trim());
      control.querySelectorAll("input").forEach((input) => {
        input.checked = values.includes(String(input.value));
      });
      return;
    }
    control.value = String(value);
  }

  function updateManualMode(form) {
    const selected = form.querySelector("input[data-manual-mode]:checked");
    const mode = selected ? selected.value : "raw";
    form.querySelectorAll("[data-manual-mode-panel]").forEach((panel) => {
      const visible = panel.dataset.manualModePanel === mode;
      panel.classList.toggle("is-hidden", !visible);
      panel.querySelectorAll("input, select, textarea").forEach((control) => {
        control.disabled = !visible;
      });
    });
    applyFieldConditions(form);
  }

  function renderManualFields(form) {
    const target = form.querySelector("[data-manual-fields]");
    const typeSelect = form.querySelector("[data-manual-puzzle-type]");
    const note = form.querySelector("[data-manual-builder-note]");
    if (!target || !typeSelect) return;
    const schema = schemas[typeSelect.value] || {};
    const fields = schema.manual_editor_fields || [];
    const existingValues = manualEditorValues();
    target.innerHTML = "";
    if (note) {
      note.textContent = schema.label
        ? `${schema.label} controls are supplied by the puzzle module. Save as a draft, preview, adjust, then publish.`
        : "Choose a puzzle type to load its builder.";
    }
    if (!schema.supports_manual_editor || fields.length === 0) {
      const missingNote = document.createElement("p");
      missingNote.className = "muted";
      missingNote.textContent = "This puzzle type does not have a module builder yet. Use Advanced Raw Payloads for now.";
      target.appendChild(missingNote);
      const rawMode = form.querySelector('input[data-manual-mode][value="raw"]');
      if (rawMode) rawMode.checked = true;
      updateManualMode(form);
      return;
    }
    fields.forEach((field) => {
      const label = document.createElement("label");
      label.textContent = field.label;
      const control = setFieldPrefix(fieldControl(field), field, "manual_");
      setControlValue(control, field, existingValues[field.name]);
      label.appendChild(control);
      if (field.help_text) {
        const help = document.createElement("small");
        help.className = "muted";
        help.textContent = field.help_text;
        label.appendChild(help);
      }
      target.appendChild(label);
    });
    applyFieldConditions(form);
    renderSudokuProgressivePreview(form, true);
  }

  manualForms.forEach((form) => {
    form.addEventListener('input', () => renderSudokuProgressivePreview(form, true));
    form.addEventListener('change', () => { applyFieldConditions(form); renderSudokuProgressivePreview(form, true); });
    renderManualFields(form);
    updateManualMode(form);
    const typeSelect = form.querySelector("[data-manual-puzzle-type]");
    if (typeSelect) {
      typeSelect.addEventListener("change", () => {
        renderManualFields(form);
        updateManualMode(form);
      });
    }
    form.querySelectorAll("input[data-manual-mode]").forEach((input) => {
      input.addEventListener("change", () => updateManualMode(form));
    });
  });
})();
