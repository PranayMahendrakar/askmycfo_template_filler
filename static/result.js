/* ──────────────────────────────────────────────────────────────
   result.js — drives the result page interactivity:
     - trace row toggles
     - manual mapping dropdowns + Apply button
     - AI suggest (pre-fills dropdowns)
     - AI auto-fix (one-click classify + apply)
     - last-action banner dismiss

   Robust against:
     - race conditions: every action awaits the catalog load
     - whitespace differences between AI-returned label and DOM data-label
     - the model returning an unknown pattern_id (we synthesize an option)
     - stale browser cache (the URL embeds a server-startup version param)
   Every action is console.log'd so you can debug in DevTools.
   ────────────────────────────────────────────────────────────── */
"use strict";

const SCRIPT = document.currentScript;
const JOB = SCRIPT.dataset.job;
const AI_READY = SCRIPT.dataset.aiReady === "true";

console.log("[result] boot. job=%s ai_ready=%s", JOB, AI_READY);

// ── Generic helpers ─────────────────────────────────────────────
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const statusEl = $("#aiStatus");
function flash(msg, kind = "ok") {
  if (!statusEl) { console.log("[result]", kind, msg); return; }
  statusEl.textContent = msg;
  statusEl.className = "status " + kind;
  statusEl.classList.remove("hidden");
  console.log("[result]", kind, msg);
}

// Loose match: trim, lowercase, collapse whitespace
function norm(s) { return (s || "").toString().trim().replace(/\s+/g, " ").toLowerCase(); }

// ── Trace row toggles (the filled-values table) ─────────────────
$$(".val-row").forEach(row => {
  row.addEventListener("click", () => {
    const next = row.nextElementSibling;
    if (next && next.classList.contains("val-trace")) {
      next.classList.toggle("hidden");
      row.classList.toggle("expanded");
    }
  });
});

// ── Pattern catalog loader (gates the rest of the UI) ───────────
const MAP_SECTION_ORDER = [
  ["equity",           "Equity / Networth"],
  ["current_liab",     "Current Liabilities"],
  ["noncurrent_liab",  "Non-Current Liabilities"],
  ["current_asset",    "Current Assets"],
  ["noncurrent_asset", "Non-Current Assets"],
  ["pl",               "Profit & Loss"],
];

let PATTERNS = {};
let _patternsReady = null;   // Promise that resolves after the catalog is loaded

function patternsReady() {
  if (_patternsReady) return _patternsReady;
  _patternsReady = (async () => {
    try {
      const res = await fetch("/api/patterns");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const d = await res.json();
      PATTERNS = d.patterns || {};
      console.log("[result] loaded %d patterns", Object.keys(PATTERNS).length);
      populateDropdowns();
    } catch (e) {
      console.error("[result] /api/patterns failed:", e);
      flash("Could not load pattern catalog: " + e.message + ". Try reloading the page.", "error");
      // Resolve anyway so dependent code can degrade gracefully
    }
  })();
  return _patternsReady;
}

function populateDropdowns() {
  const buckets = {};
  for (const [pid, p] of Object.entries(PATTERNS)) {
    (buckets[p.group] = buckets[p.group] || []).push({ pid, ...p });
  }
  $$(".map-select").forEach(sel => {
    // Build optgroups
    for (const [groupId, groupLabel] of MAP_SECTION_ORDER) {
      const items = buckets[groupId] || [];
      if (!items.length) continue;
      const og = document.createElement("optgroup");
      og.label = groupLabel;
      for (const it of items.sort((a, b) => a.name.localeCompare(b.name))) {
        const opt = document.createElement("option");
        opt.value = it.pid;
        opt.textContent = it.name;
        og.appendChild(opt);
      }
      sel.appendChild(og);
    }
    sel.addEventListener("change", updateMappingSummary);
  });
  updateMappingSummary();
}

function updateMappingSummary() {
  const selects = $$(".map-select");
  const mapped  = selects.filter(s => s.value).length;
  const summary = $("#mappingSummary");
  const btn     = $("#applyManualBtn");
  if (!summary || !btn) return;
  if (mapped === 0) {
    summary.textContent = "No mappings selected.";
    btn.disabled = true;
  } else {
    summary.textContent = `${mapped} mapping${mapped !== 1 ? 's' : ''} selected.`;
    btn.disabled = false;
  }
}

// ── Apply manual mappings ───────────────────────────────────────
const applyBtn = $("#applyManualBtn");
if (applyBtn) {
  applyBtn.addEventListener("click", async () => {
    await patternsReady();
    const mappings = [];
    $$("#unmatchedTable tbody tr").forEach(tr => {
      const sel = tr.querySelector(".map-select");
      if (sel && sel.value) {
        mappings.push({ label: tr.dataset.label, pattern_id: sel.value });
      }
    });
    console.log("[result] manual_fix: %d mappings", mappings.length, mappings);
    if (!mappings.length) return;
    applyBtn.disabled = true;
    applyBtn.textContent = `Applying ${mappings.length} mapping(s)…`;
    flash(`Adding ${mappings.length} label(s) to your CSV dictionary and re-running…`, "ok");
    try {
      const res = await fetch("/api/manual_fix", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job: JOB, mappings }),
      });
      const json = await res.json();
      console.log("[result] manual_fix response:", json);
      if (!res.ok || !json.ok) {
        flash("Failed: " + (json.error || "unknown") +
              ". Open the Flask console for details.", "error");
        applyBtn.disabled = false;
        applyBtn.textContent = "Apply mappings & re-run";
        return;
      }
      flash(json.summary, "ok");
      setTimeout(() => window.location.reload(), 800);
    } catch (e) {
      console.error("[result] manual_fix error:", e);
      flash("Network error: " + e.message, "error");
      applyBtn.disabled = false;
      applyBtn.textContent = "Apply mappings & re-run";
    }
  });
}

// ── AI: suggest mappings ────────────────────────────────────────
// Robust: awaits PATTERNS, falls back to trimmed/lower-cased label match
// against the table rows, and synthesises a dropdown option if the AI
// returns a pattern_id that isn't already in the <select>.
const suggestBtn = $("#aiSuggestBtn");
if (suggestBtn) {
  suggestBtn.addEventListener("click", async () => {
    await patternsReady();
    if (!Object.keys(PATTERNS).length) {
      flash("Pattern catalog hasn't loaded yet — reload the page and try again.", "error");
      return;
    }
    const original = suggestBtn.textContent;
    suggestBtn.disabled = true;
    suggestBtn.textContent = "💡 Asking AI…";
    flash("Asking your model to suggest a pattern for each unmatched row… (3-10 seconds)", "ok");
    try {
      const res = await fetch("/api/ai/suggest_unmatched", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job: JOB }),
      });
      const j = await res.json();
      console.log("[result] suggest response:", j);
      if (!res.ok || !j.ok) {
        flash("Suggest failed: " + (j.error || "unknown"), "error");
        return;
      }
      const suggestions = j.suggestions || [];
      let prefilled = 0, missed = 0;

      // Build a label -> tr index, normalised, so whitespace can't kill the match
      const trByLabel = new Map();
      $$("#unmatchedTable tbody tr").forEach(tr => {
        trByLabel.set(norm(tr.dataset.label), tr);
      });

      for (const s of suggestions) {
        if (!s.pattern_id) {
          console.log("[result] suggest: no pattern_id for", s.label);
          continue;
        }
        const tr = trByLabel.get(norm(s.label));
        if (!tr) {
          console.warn("[result] suggest: no row in DOM for label", JSON.stringify(s.label));
          missed += 1;
          continue;
        }
        const sel = tr.querySelector(".map-select");
        if (!sel) { missed += 1; continue; }

        // If the catalog has this pattern_id, set the value. If the dropdown
        // doesn't already have it as an option (shouldn't happen, but be
        // defensive), synthesise one so the value sticks.
        if (PATTERNS[s.pattern_id]) {
          const hasOption = !![...sel.options].find(o => o.value === s.pattern_id);
          if (!hasOption) {
            const opt = document.createElement("option");
            opt.value = s.pattern_id;
            opt.textContent = PATTERNS[s.pattern_id].name + " ⚡";
            sel.appendChild(opt);
          }
          sel.value = s.pattern_id;
          sel.classList.add("ai-suggested");
          sel.title = `AI suggested with confidence ${(s.confidence || 0).toFixed(2)}` +
                      (s.reasoning ? `: ${s.reasoning}` : "");
          prefilled += 1;
        } else {
          console.warn("[result] suggest: AI returned unknown pattern_id",
                       JSON.stringify(s.pattern_id), "for", JSON.stringify(s.label));
          missed += 1;
        }
      }

      // Tell the user clearly what happened
      const skippedNull = suggestions.filter(s => !s.pattern_id).length;
      let detail = `AI pre-filled ${prefilled} of ${suggestions.length} mapping(s).`;
      if (skippedNull > 0) {
        detail += ` ${skippedNull} had no good match (left as Skip).`;
      }
      if (missed > 0) {
        detail += ` ${missed} couldn't be applied (unknown pattern or row removed).`;
      }
      flash(detail + " Review and click Apply mappings & re-run.", "ok");
      updateMappingSummary();
    } catch (e) {
      console.error("[result] suggest error:", e);
      flash("Network error: " + e.message, "error");
    } finally {
      suggestBtn.disabled = false;
      suggestBtn.textContent = original;
    }
  });
}

// ── AI: full auto-fix (classify + apply automatically) ──────────
const aiBtn = $("#aiFixBtn");
if (aiBtn) {
  aiBtn.addEventListener("click", async () => {
    await patternsReady();
    aiBtn.disabled = true;
    const original = aiBtn.textContent;
    aiBtn.textContent = "🪄 Classifying…";
    flash("Sending unmatched labels to your model and applying high-confidence matches…", "ok");
    try {
      const res = await fetch("/api/ai/auto_fix", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job: JOB }),
      });
      const j = await res.json();
      console.log("[result] auto_fix response:", j);
      if (!res.ok || !j.ok) {
        flash("AI auto-fix failed: " + (j.error || "unknown"), "error");
        aiBtn.disabled = false;
        aiBtn.textContent = original;
        return;
      }
      flash(j.summary || "Done. Reloading…", "ok");
      setTimeout(() => window.location.reload(), 800);
    } catch (e) {
      console.error("[result] auto_fix error:", e);
      flash("Network error: " + e.message, "error");
      aiBtn.disabled = false;
      aiBtn.textContent = original;
    }
  });
}

// ── Last-action banner: dismiss ─────────────────────────────────
const dismissBtn = $("#dismissLastAction");
if (dismissBtn) {
  dismissBtn.addEventListener("click", () => {
    const card = $("#lastActionBanner");
    if (card) card.classList.add("hidden");
  });
}

// ── Boot ─────────────────────────────────────────────────────────
if ($(".map-select")) {
  patternsReady();   // start loading immediately
}
