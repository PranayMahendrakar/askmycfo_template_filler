/* ─────────────────────────────────────────────────────────────
   keywords.js — the keyword dictionary editor.
   Talks to /api/keywords (GET, /add, /delete).

   Goals:
     - One card per pattern, grouped by section.
     - Add a keyword: type → Enter or click Add. Appends to the CSV.
     - Delete a keyword: click the × beside it. Removes from CSV.
     - Hand-curated extras shown read-only (they live in pattern_specs.json,
       not in the editable CSVs).
   ───────────────────────────────────────────────────────────── */

const KW = {
  patterns: [],   // [{pattern_id, name, group, csv_source, csv_category, ...}]
  groups: {},     // {group_id: "Display Name"}
  filter: "",
};

const GROUP_ORDER = [
  "equity", "current_liab", "noncurrent_liab",
  "current_asset", "noncurrent_asset", "pl",
];

// ─── Boot ────────────────────────────────────────────────────

async function boot() {
  try {
    const res = await fetch("/api/keywords");
    const json = await res.json();
    KW.patterns = json.patterns || [];
    KW.groups   = json.groups   || {};
    render();
  } catch (e) {
    flash("Failed to load: " + e.message, "error");
  }
  document.getElementById("kwFilter").addEventListener("input", e => {
    KW.filter = e.target.value.trim().toLowerCase();
    render();
  });
}

// ─── Render ──────────────────────────────────────────────────

function render() {
  const root = document.getElementById("kwGroups");
  root.innerHTML = "";

  const bucket = {};
  for (const p of KW.patterns) {
    if (!matchesFilter(p)) continue;
    (bucket[p.group] = bucket[p.group] || []).push(p);
  }

  const ordered = [...GROUP_ORDER, ...Object.keys(bucket).filter(g => !GROUP_ORDER.includes(g))];

  let totalPatterns = 0;
  let totalKeywords = 0;

  for (const g of ordered) {
    if (!bucket[g]) continue;
    const node = document.getElementById("kwGroupTpl").content.cloneNode(true);
    node.querySelector(".section-head").textContent = KW.groups[g] || g;
    const cards = node.querySelector(".kw-cards");
    for (const p of bucket[g]) {
      cards.appendChild(renderCard(p));
      totalPatterns += 1;
      totalKeywords += p.n_csv;
    }
    root.appendChild(node);
  }

  document.getElementById("kwStats").textContent =
    `${totalPatterns} patterns · ${totalKeywords.toLocaleString()} CSV keywords`;
}

function matchesFilter(p) {
  if (!KW.filter) return true;
  const hay = [
    p.pattern_id, p.name, p.csv_category || "", p.csv_source || "",
    ...(p.csv_keywords || []), ...(p.extras || []),
  ].join(" ").toLowerCase();
  return hay.includes(KW.filter);
}

function renderCard(p) {
  const node = document.getElementById("kwCardTpl").content.cloneNode(true);
  const card = node.querySelector(".kw-card");
  card.dataset.patternId = p.pattern_id;

  card.querySelector(".kw-name").textContent = p.name;

  // Source pill: which CSV this pattern is wired to
  const sourcePill = card.querySelector(".kw-source-pill");
  const csvPill    = card.querySelector(".kw-csv-pill");
  const counts     = card.querySelector(".kw-counts");
  if (p.csv_source) {
    sourcePill.textContent = p.csv_source.toUpperCase() + " csv";
    csvPill.textContent = "▸ " + p.csv_category;
    counts.textContent = `${p.n_csv} csv · ${p.n_extras} extras`;
  } else {
    sourcePill.textContent = "hand-curated";
    sourcePill.classList.add("hand-curated-pill");
    csvPill.style.display = "none";
    counts.textContent = `${p.n_extras} extras only`;
  }

  // Add row
  const input = card.querySelector(".kw-input");
  const addBtn = card.querySelector(".kw-add");
  if (!p.csv_source) {
    input.disabled = true;
    input.placeholder = "Hand-curated pattern — extras live in pattern_specs.json";
    addBtn.disabled = true;
  } else {
    const doAdd = async () => {
      const kw = input.value.trim();
      if (!kw) return;
      addBtn.disabled = true;
      try {
        const res = await fetch("/api/keywords/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pattern_id: p.pattern_id, keyword: kw }),
        });
        const json = await res.json();
        if (!json.ok) {
          flash("Add failed: " + (json.error || "unknown"), "error");
        } else if (!json.added) {
          flash(`'${kw}' is already in this pattern (duplicate).`, "ok");
          input.value = "";
        } else {
          flash(`Added '${kw}' to ${p.name}.`, "ok");
          input.value = "";
          // Optimistic local update — append to the list immediately
          p.csv_keywords.push(kw);
          p.n_csv = p.csv_keywords.length;
          render();
        }
      } catch (e) {
        flash("Network error: " + e.message, "error");
      } finally {
        addBtn.disabled = false;
      }
    };
    addBtn.addEventListener("click", doAdd);
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); doAdd(); }
    });
  }

  // CSV list
  const csvUl = card.querySelector(".csv-list");
  const csvCount = card.querySelector(".kw-list-count");
  csvCount.textContent = ` (${(p.csv_keywords || []).length})`;
  for (const kw of (p.csv_keywords || [])) {
    csvUl.appendChild(renderKeywordItem(p, kw, /*editable*/ !!p.csv_source));
  }
  if (!p.csv_keywords || p.csv_keywords.length === 0) {
    const li = document.createElement("li");
    li.className = "kw-empty";
    li.textContent = p.csv_source
      ? "No keywords yet — add the first one above."
      : "No CSV bucket — relies on hand-curated extras only.";
    csvUl.appendChild(li);
  }

  // Extras list (read-only)
  const extrasUl = card.querySelector(".extras-list");
  const extrasCount = card.querySelector(".kw-extras-count");
  extrasCount.textContent = ` (${(p.extras || []).length})`;
  for (const kw of (p.extras || [])) {
    extrasUl.appendChild(renderKeywordItem(p, kw, /*editable*/ false));
  }
  if (!p.extras || p.extras.length === 0) {
    const li = document.createElement("li");
    li.className = "kw-empty";
    li.textContent = "No hand-curated extras.";
    extrasUl.appendChild(li);
  }

  return node;
}

function renderKeywordItem(p, kw, editable) {
  const li = document.createElement("li");
  li.className = "kw-item" + (editable ? "" : " kw-item-readonly");
  const span = document.createElement("span");
  span.className = "kw-text";
  span.textContent = kw;
  li.appendChild(span);
  if (editable) {
    const btn = document.createElement("button");
    btn.className = "btn-ghost-icon kw-remove";
    btn.title = "Remove from CSV";
    btn.textContent = "✕";
    btn.addEventListener("click", async () => {
      if (!confirm(`Remove "${kw}" from ${p.name}?`)) return;
      try {
        const res = await fetch("/api/keywords/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pattern_id: p.pattern_id, keyword: kw }),
        });
        const json = await res.json();
        if (!json.ok) {
          flash("Delete failed: " + (json.error || "unknown"), "error");
          return;
        }
        if (!json.removed) {
          flash(`'${kw}' wasn't found (already removed?).`, "error");
        } else {
          flash(`Removed '${kw}' from ${p.name}.`, "ok");
        }
        p.csv_keywords = p.csv_keywords.filter(k => k.toLowerCase() !== kw.toLowerCase());
        p.n_csv = p.csv_keywords.length;
        render();
      } catch (e) {
        flash("Network error: " + e.message, "error");
      }
    });
    li.appendChild(btn);
  }
  return li;
}

// ─── Status ──────────────────────────────────────────────────

function flash(msg, kind = "ok") {
  const s = document.getElementById("status");
  s.textContent = msg;
  s.className = "status " + kind;
  s.classList.remove("hidden");
  clearTimeout(window.__flashTimer);
  window.__flashTimer = setTimeout(() => s.classList.add("hidden"), 4000);
}

boot();
