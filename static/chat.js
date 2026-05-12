/* ──────────────────────────────────────────────────────────────
   chat.js — floating chat assistant. Talks to /api/ai/chat.

   - History persisted in localStorage so it survives page reloads
     (which the result page does after each manual or AI fix).
   - Shows tool-call summaries inline ("✓ Added '(b) Provisions' to
     provisions_current").
   - Auto-reloads the host page if any write tool succeeded, so the
     freshly-saved rule/keyword is reflected on screen immediately.
   ────────────────────────────────────────────────────────────── */

const STORAGE_KEY = "amcChatHistory.v1";
const widget   = document.getElementById("chatWidget");
const openBtn  = document.getElementById("chatOpen");
const closeBtn = document.getElementById("chatClose");
const clearBtn = document.getElementById("chatClear");
const body     = document.getElementById("chatBody");
const form     = document.getElementById("chatForm");
const input    = document.getElementById("chatInput");

let history = loadHistory();

function loadHistory() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch (e) {}
  return [];
}
function saveHistory() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(history)); }
  catch (e) {}
}

// ── Open / close ─────────────────────────────────────────────
openBtn.addEventListener("click", () => {
  widget.classList.remove("hidden");
  openBtn.classList.add("hidden");
  renderHistory();
  setTimeout(() => input.focus(), 80);
});
closeBtn.addEventListener("click", () => {
  widget.classList.add("hidden");
  openBtn.classList.remove("hidden");
});
clearBtn.addEventListener("click", () => {
  if (!confirm("Clear the conversation history?")) return;
  history = [];
  saveHistory();
  renderHistory();
});

// ── Render ───────────────────────────────────────────────────
function renderHistory() {
  if (!history.length) {
    body.innerHTML = `
      <div class="chat-empty">
        <p>I can edit your keyword dictionary and rule set in plain English.</p>
        <p class="hint">Try things like:</p>
        <ul>
          <li><em>"Add '(b) Provisions' as a keyword for provisions current"</em></li>
          <li><em>"Create a rule called EBITDA = Revenue − Cost of goods sold − Employee benefits − Depreciation − Other expenses + Other income"</em></li>
          <li><em>"What patterns are in the equity section?"</em></li>
        </ul>
      </div>`;
    return;
  }
  body.innerHTML = "";
  for (const msg of history) {
    if (msg.role === "user" || msg.role === "assistant") {
      const div = document.createElement("div");
      div.className = "chat-msg chat-" + msg.role;
      div.innerHTML = `<div class="chat-bubble">${escapeHtml(msg.content).replace(/\n/g, "<br>")}</div>`;
      body.appendChild(div);
    } else if (msg.role === "actions") {
      const div = document.createElement("div");
      div.className = "chat-actions";
      div.innerHTML = msg.content.map(a => renderAction(a)).join("");
      body.appendChild(div);
    }
  }
  body.scrollTop = body.scrollHeight;
}

function renderAction(a) {
  const ok = a.result && a.result.ok;
  const dot = ok ? "✓" : "✗";
  const kind = ok ? "ok" : "err";
  let summary = a.tool;
  const r = a.result || {};
  if (a.tool === "add_keyword" && ok) {
    summary = `Added <code>${escapeHtml(a.args.keyword)}</code> to <strong>${escapeHtml(a.args.pattern_id)}</strong>${r.note === "duplicate" ? " (already existed)" : ""}`;
  } else if (a.tool === "delete_keyword" && ok) {
    summary = `Removed <code>${escapeHtml(a.args.keyword)}</code> from <strong>${escapeHtml(a.args.pattern_id)}</strong>${r.removed ? "" : " (not found)"}`;
  } else if (a.tool === "set_rule" && ok) {
    summary = `${r.operation === 'created' ? 'Created' : 'Updated'} rule <strong>${escapeHtml(a.args.label)}</strong> (${r.operands_count} operand${r.operands_count !== 1 ? 's' : ''})`;
  } else if (a.tool === "delete_rule" && ok) {
    summary = `Deleted rule <strong>${escapeHtml(a.args.label)}</strong>`;
  } else if (!ok) {
    summary = `${a.tool} failed: ${escapeHtml(r.error || 'unknown')}`;
  } else {
    summary = `${a.tool}`;  // read-only tools (list_*, find_*)
  }
  return `<div class="chat-action chat-action-${kind}"><span class="chat-action-dot">${dot}</span>${summary}</div>`;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c]));
}

// ── Submit ───────────────────────────────────────────────────
form.addEventListener("submit", async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  history.push({ role: "user", content: text });
  saveHistory();
  renderHistory();
  input.value = "";
  input.disabled = true;

  // Placeholder while waiting
  const wait = document.createElement("div");
  wait.className = "chat-msg chat-assistant chat-pending";
  wait.innerHTML = `<div class="chat-bubble"><em>Thinking…</em></div>`;
  body.appendChild(wait);
  body.scrollTop = body.scrollHeight;

  try {
    // Send only the user/assistant pairs (drop our "actions" pseudo-rows)
    const messagesForServer = history.filter(m => m.role === "user" || m.role === "assistant");
    const res = await fetch("/api/ai/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: messagesForServer }),
    });
    const j = await res.json();
    wait.remove();
    if (!res.ok || !j.ok) {
      history.push({ role: "assistant", content: "⚠️ " + (j.error || "Request failed.") });
      saveHistory(); renderHistory();
      return;
    }
    if (j.actions && j.actions.length) {
      history.push({ role: "actions", content: j.actions });
    }
    history.push({ role: "assistant", content: j.reply || "(no reply)" });
    saveHistory(); renderHistory();

    // If any write-action succeeded, reload the page so the user sees
    // the result without needing to manually refresh.
    const anyWrite = (j.actions || []).some(a => {
      return a.result && a.result.ok &&
             ["add_keyword","delete_keyword","set_rule","delete_rule"].includes(a.tool);
    });
    if (anyWrite && location.pathname.match(/^\/(rules|keywords)\/?$/)) {
      setTimeout(() => location.reload(), 1200);
    }
  } catch (e) {
    wait.remove();
    history.push({ role: "assistant", content: "⚠️ Network error: " + e.message });
    saveHistory(); renderHistory();
  } finally {
    input.disabled = false;
    input.focus();
  }
});

// Enter sends, Shift+Enter newline
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// Pre-render once in case widget is opened with existing history
renderHistory();
