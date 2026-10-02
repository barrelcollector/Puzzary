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
    return control;
  }

  function renderFields(form, schema) {
    const target = form.querySelector("[data-generation-fields]");
    if (!target) return;
    target.innerHTML = "";
    (schema.generation_fields || []).forEach((field) => {
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

  function updateForm(form) {
    const typeSelect = form.querySelector("[data-puzzle-type]");
    const schema = schemas[typeSelect ? typeSelect.value : ""] || {};
    const needsWordBank = Boolean(schema.needs_word_bank);
    form.querySelectorAll('[data-source-group="word_bank"]').forEach((group) => setGroupVisible(group, needsWordBank));
    form.querySelectorAll('[data-source-group="theme"]').forEach((group) => setGroupVisible(group, !needsWordBank));
    renderFields(form, schema);
  }

  forms.forEach((form) => {
    form.querySelectorAll('[data-source-group="theme"] input').forEach((input) => {
      input.setAttribute("data-required-when-visible", "true");
    });
    updateForm(form);
    const typeSelect = form.querySelector("[data-puzzle-type]");
    if (typeSelect) typeSelect.addEventListener("change", () => updateForm(form));
  });
})();
