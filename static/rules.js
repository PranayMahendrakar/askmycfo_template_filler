/* ─────────────────────────────────────────────────────────────
   Rule editor — dropdowns + operand list per rule, grouped by
   section. Talks to /api/rules, /api/patterns, /api/cell_map.

   Data shape (rules.json):
     {
       "<Template Field>": {
         "section": "Current Assets",
         "operands": [
           {"pattern": "<pattern_id>", "op": "+|-|*|/"},
           ...
         ]
       },
       ...
     }
   ───────────────────────────────────────────────────────────── */

const STATE = {
  patterns: {},          // {pid: {name, group, aliases, section_filter}}
  groups:   {},          // {group_id: "Display Name"}
  cellMap:  {},          // {label: {row, section}}
  rules:    {},          // editable rules dict (the live form state)
  dirty:    false,
};

const SECTION_ORDER = [
  "Networth",
  "Current Liabilities",
  "Non-Current Liabilities",
  "Current Assets",
  "Non-Current Assets",
  "P&L",
];

// ─── Boot ────────────────────────────────────────────────────

async function boot() {
  const [pRes, cRes, rRes] = await Promise.all([
    fetch("/api/patterns"), fetch("/api/cell_map"), fetch("/api/rules"),
  ]);
  const patternsDoc = await pRes.json();
  STATE.patterns = patternsDoc.patterns || {};
  STATE.groups   = patternsDoc._groups   || {};
  STATE.cellMap  = (await cRes.json()).data_rows || {};
  STATE.rules    = await rRes.json();
  render();
  hookToolbar();
}

// ─── Render ──────────────────────────────────────────────────

function render() {
  const root = document.getElementById("ruleListBySection");
  root.innerHTML = "";

  // Bucket the rules by their section so each Balance Sheet block lives
  // under its own heading. Anything with no section, or an unrecognized
  // one, lands under "Other".
  const bucket = {};
  for (const [label, rule] of Object.entries(STATE.rules)) {
    if (label.startsWith("_")) continue;
    const sec = (rule.section || STATE.cellMap[label]?.section || "Other").trim();
    (bucket[sec] = bucket[sec] || []).push([label, rule]);
  }

  const ordered = [...SECTION_ORDER, ...Object.keys(bucket).filter(s => !SECTION_ORDER.includes(s))];

  for (const sec of ordered) {
    if (!bucket[sec] || bucket[sec].length === 0) continue;
    const head = document.getElementById("sectionHeadTpl").content.cloneNode(true);
    head.querySelector(".section-head").textContent = sec;
    root.appendChild(head);
    for (const [label, rule] of bucket[sec]) {
      root.appendChild(renderRuleCard(label, rule));
    }
  }
}

function renderRuleCard(label, rule) {
  const node = document.getElementById("ruleCardTpl").content.cloneNode(true);
  const card = node.querySelector(".rule-card");
  card.dataset.label = label;

  const targetInput = card.querySelector(".rule-target");
  targetInput.value = label;
  targetInput.addEventListener("change", () => {
    const newLabel = targetInput.value.trim();
    if (!newLabel || newLabel === label) return;
    if (STATE.rules[newLabel]) {
      flash(`A rule named "${newLabel}" already exists. Renaming reverted.`, "error");
      targetInput.value = label;
      return;
    }
    STATE.rules[newLabel] = STATE.rules[label];
    delete STATE.rules[label];
    markDirty();
    render();
  });

  // Hint line: cell ref + section
  const cellHint = card.querySelector(".rule-cell");
  const secHint  = card.querySelector(".rule-section-pill");
  const mapping  = STATE.cellMap[label];
  if (mapping) {
    cellHint.textContent = `Row ${mapping.row} · F${mapping.row}/G${mapping.row}`;
    secHint.textContent  = mapping.section;
  } else {
    cellHint.textContent = "Unmapped — will be ignored on output";
    cellHint.classList.add("unmapped");
    secHint.textContent  = rule.section || "";
  }

  // Formula preview row
  const formulaEl = card.querySelector(".rule-formula");
  card._renderFormula = () => updateFormulaPreview(formulaEl, label, rule);

  // Operands
  const list = card.querySelector(".operand-list");
  for (const op of (rule.operands || [])) {
    list.appendChild(renderOperandRow(label, op, card));
  }

  card.querySelector(".add-operand-btn").addEventListener("click", () => {
    if (!rule.operands) rule.operands = [];
    const newOp = { pattern: firstPatternId(), op: "+" };
    rule.operands.push(newOp);
    list.appendChild(renderOperandRow(label, newOp, card));
    card._renderFormula();
    markDirty();
  });

  card.querySelector(".delete-rule").addEventListener("click", () => {
    if (!confirm(`Delete the rule for "${label}"?`)) return;
    delete STATE.rules[label];
    markDirty();
    render();
  });

  // Initial preview
  card._renderFormula();

  return node;
}

function updateFormulaPreview(el, label, rule) {
  // Renders a plain-English-ish formula line:
  //   Accounts Payable = Trade Payables
  //   Other expenses less other income = Other Income − Other Expenses
  el.innerHTML = "";
  const labelSpan = document.createElement("span");
  labelSpan.className = "f-label";
  labelSpan.textContent = label || "(unnamed)";
  el.appendChild(labelSpan);

  const eq = document.createElement("span");
  eq.className = "f-eq"; eq.textContent = "=";
  el.appendChild(eq);

  const operands = rule.operands || [];
  if (operands.length === 0) {
    const empty = document.createElement("span");
    empty.className = "f-empty";
    empty.textContent = "(no operands — value will be 0)";
    el.appendChild(empty);
    return;
  }

  const opGlyph = { "+": "+", "-": "−", "*": "×", "/": "÷" };

  operands.forEach((op, i) => {
    const opStr = op.op || "+";
    if (i === 0) {
      // Seed: only show "−" if it's negative. "+" hidden, "*" / "/" hidden.
      if (opStr === "-") {
        const sign = document.createElement("span");
        sign.className = "f-op";
        sign.textContent = "−";
        el.appendChild(sign);
      }
    } else {
      const glyph = document.createElement("span");
      glyph.className = "f-op";
      glyph.textContent = opGlyph[opStr] || opStr;
      el.appendChild(glyph);
    }
    const pat = document.createElement("span");
    pat.className = "f-pattern";
    const p = STATE.patterns[op.pattern];
    pat.textContent = p ? p.name : (op.pattern || "(unset)");
    el.appendChild(pat);
  });
}

function renderOperandRow(ruleLabel, opEntry, card) {
  const node = document.getElementById("operandRowTpl").content.cloneNode(true);
  const row  = node.querySelector(".operand-row");

  // Operator
  const opSel = row.querySelector(".op-select");
  opSel.value = opEntry.op || "+";
  opSel.addEventListener("change", () => {
    opEntry.op = opSel.value;
    if (card && card._renderFormula) card._renderFormula();
    markDirty();
  });

  // Pattern — grouped <optgroup> by section so the dropdown is browsable
  const patSel = row.querySelector(".pattern-select");
  buildPatternOptions(patSel, opEntry.pattern);
  patSel.addEventListener("change", () => {
    opEntry.pattern = patSel.value;
    if (card && card._renderFormula) card._renderFormula();
    markDirty();
  });

  // Remove
  row.querySelector(".remove-operand").addEventListener("click", () => {
    const rule = STATE.rules[ruleLabel];
    if (!rule || !rule.operands) return;
    const idx = rule.operands.indexOf(opEntry);
    if (idx >= 0) rule.operands.splice(idx, 1);
    row.remove();
    if (card && card._renderFormula) card._renderFormula();
    markDirty();
  });

  return node;
}

function buildPatternOptions(sel, selected) {
  sel.innerHTML = "";
  // group → patterns within group
  const byGroup = {};
  for (const [pid, p] of Object.entries(STATE.patterns)) {
    const g = p.group || "other";
    (byGroup[g] = byGroup[g] || []).push([pid, p]);
  }
  // Preserve a sensible group order: walk _groups dict order, then anything else.
  const groupOrder = [...Object.keys(STATE.groups), ...Object.keys(byGroup).filter(g => !STATE.groups[g])];
  for (const g of groupOrder) {
    if (!byGroup[g]) continue;
    const og = document.createElement("optgroup");
    og.label = STATE.groups[g] || g;
    byGroup[g].sort((a, b) => a[1].name.localeCompare(b[1].name));
    for (const [pid, p] of byGroup[g]) {
      const o = document.createElement("option");
      o.value = pid;
      o.textContent = p.name;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  if (selected) sel.value = selected;
}

function firstPatternId() {
  return Object.keys(STATE.patterns)[0] || "";
}

// ─── Toolbar ─────────────────────────────────────────────────

function hookToolbar() {
  document.getElementById("saveBtn").addEventListener("click", saveAll);
  document.getElementById("addRuleBtn").addEventListener("click", addRule);
  document.getElementById("resetBtn").addEventListener("click", resetDefaults);
  window.addEventListener("beforeunload", (e) => {
    if (STATE.dirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

function markDirty() {
  STATE.dirty = true;
  document.getElementById("dirty-indicator").classList.remove("hidden");
}

function markClean() {
  STATE.dirty = false;
  document.getElementById("dirty-indicator").classList.add("hidden");
}

function flash(msg, kind = "ok") {
  const s = document.getElementById("status");
  s.textContent = msg;
  s.className = "status " + kind;
  s.classList.remove("hidden");
  clearTimeout(window.__flashTimer);
  window.__flashTimer = setTimeout(() => s.classList.add("hidden"), 4500);
}

async function saveAll() {
  // Strip empty rules (no operands) so the user doesn't accidentally save
  // a broken rule that contributes nothing but takes a spot in the UI.
  const body = {};
  for (const [label, rule] of Object.entries(STATE.rules)) {
    if (label.startsWith("_")) { body[label] = rule; continue; }
    if (!rule.operands || rule.operands.length === 0) continue;
    body[label] = {
      section: rule.section || (STATE.cellMap[label]?.section ?? ""),
      operands: rule.operands.map(o => ({ pattern: o.pattern, op: o.op || "+" })),
    };
  }

  try {
    const res = await fetch("/api/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const json = await res.json();
    if (!json.ok) {
      flash("Save failed: " + (json.errors ? json.errors.join("; ") : json.error || "unknown"), "error");
      return;
    }
    flash(`Saved ${json.n_rules} rules.`, "ok");
    STATE.rules = body;
    markClean();
  } catch (e) {
    flash("Network error: " + e.message, "error");
  }
}

function addRule() {
  // Find a template label that doesn't have a rule yet
  const taken = new Set(Object.keys(STATE.rules));
  const candidates = Object.keys(STATE.cellMap).filter(k => !taken.has(k));
  let label;
  if (candidates.length) {
    label = candidates[0];
  } else {
    let i = 1;
    while (STATE.rules[`New rule ${i}`]) i++;
    label = `New rule ${i}`;
  }
  STATE.rules[label] = {
    section: STATE.cellMap[label]?.section || "",
    operands: [{ pattern: firstPatternId(), op: "+" }],
  };
  markDirty();
  render();
  // Scroll to and focus the new card
  setTimeout(() => {
    const card = [...document.querySelectorAll(".rule-card")]
      .find(c => c.dataset.label === label);
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.querySelector(".rule-target")?.focus();
    }
  }, 50);
}

async function resetDefaults() {
  if (!confirm("Reset all rules to the bundled defaults? This cannot be undone (your current rules will be lost unless you saved a backup).")) {
    return;
  }
  try {
    const res = await fetch("/api/rules/reset", { method: "POST" });
    const json = await res.json();
    if (!json.ok) {
      flash("Reset failed: " + (json.error || "unknown"), "error");
      return;
    }
    flash("Rules restored to defaults.", "ok");
    markClean();
    // Reload from server
    STATE.rules = await (await fetch("/api/rules")).json();
    render();
  } catch (e) {
    flash("Network error: " + e.message, "error");
  }
}

// ─── Go ──────────────────────────────────────────────────────

boot().catch(e => {
  document.getElementById("ruleListBySection").innerHTML =
    `<p class="status error">Failed to load: ${e.message}</p>`;
});
