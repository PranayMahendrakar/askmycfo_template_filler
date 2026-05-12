"""
ai_assistant.py -- conversational agent that edits the rule set and the
keyword dictionary via tool calls.

Why
---
The /keywords and /rules editors are good for power users. Many of the
edits, though, are easier in plain English:

    "Add '(b) Provisions' under provisions current"
    "Create a rule called EBITDA = Revenue - COGS - Employee benefits"
    "What patterns are in the equity section?"
    "Remove the keyword 'foo bar' from trade payables"

This module wraps OpenAI's function-calling API and exposes a small,
audited set of tools backed by the same KeywordStore + rules.json the rest
of the app uses. Every tool action returns a structured receipt so the UI
can show "Added 3 keywords; updated 1 rule" instead of just a chat blob.

Design
------
- Stateless: every /api/ai/chat call passes the full message history. The
  server doesn't track conversations.
- Auditable: actions[] returned alongside the reply describes every tool
  invocation, its arguments, and its outcome.
- Bounded: max 6 tool-calling rounds per request to prevent runaway loops.
- Safe: write tools call back into KeywordStore (which already has atomic
  writes, dup detection, .bak backup) and into the same rules validator
  used by /api/rules POST. No raw file writes.
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


log = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """No API key, missing dep, etc."""


class AssistantError(Exception):
    """Upstream call failed or returned garbage."""


# ── System prompt ──────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the editor assistant for AskMyCFO Template Filler, a tool that maps
Indian annual report line items to a template spreadsheet.

You have tools to:
- list and search the pattern catalog
- add or delete keywords (line-item variations) in the CSV dictionary
- list, set (create/update), or delete rules (formulas) in rules.json

When the user describes an edit:
1. If you need to find a pattern_id, call find_patterns or list_patterns.
2. Call the smallest set of write tools needed.
3. After all writes, reply in 1-3 sentences summarising what changed.
   Always include the exact pattern_id(s) and rule label(s) you touched.

Be concise. Don't ask follow-up questions if the request is unambiguous --
just do it and report. Only ask if you really cannot guess (e.g. user says
"add the new keyword" without saying which one or to which pattern).

The pattern catalog follows Schedule III of the Companies Act 2013 and
Ind AS. Sections are: Networth, Current Liabilities, Non-Current
Liabilities, Current Assets, Non-Current Assets, P&L. Operators in rules
are: +, -, *, /.
"""


# ── Tool registry ──────────────────────────────────────────────────

@dataclass
class ToolContext:
    """All the live state a tool needs to read/write."""
    store: Any                           # KeywordStore
    rules_file: Path
    patterns_file: Path
    rules_default_file: Path
    apply_mappings_and_rerun: Optional[Callable[[List[Dict[str, str]]], Dict]] = None
    job: Optional[str] = None


TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_patterns",
            "description": "List every pattern in the catalog with id, name, section/group, csv source, and counts. Useful for finding a pattern_id.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_patterns",
            "description": "Search patterns by partial match on id or name. Returns up to 8 candidates with their full info. Use this when you need to find a pattern_id from a fuzzy user description like 'trade payables' or 'fixed assets'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Partial name, fragment, or section to search for. Multiple words OK."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_keywords",
            "description": "List the CSV keyword variations currently stored under one pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern_id": {"type": "string"},
                },
                "required": ["pattern_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_keyword",
            "description": "Append a new keyword variation to a pattern's CSV bucket. The keyword should be the exact line-item label as it appears in annual reports (case-insensitive duplicate detection).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern_id": {"type": "string"},
                    "keyword":    {"type": "string", "description": "The line-item label, e.g. '(b) Provisions' or 'Sundry creditors - related parties'"},
                },
                "required": ["pattern_id", "keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_keyword",
            "description": "Remove a keyword variation from a pattern's CSV bucket.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern_id": {"type": "string"},
                    "keyword":    {"type": "string"},
                },
                "required": ["pattern_id", "keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_rules",
            "description": "List all rules. Returns rule labels with their section and operands.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_rule",
            "description": "Create or update a rule. If the label already exists it is replaced; otherwise a new rule is added. Operands are evaluated left-to-right, with the first operand's op being its sign.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "The template field name, e.g. 'Accounts Payable' or 'EBITDA'."},
                    "section": {"type": "string", "enum": ["Networth", "Current Liabilities", "Non-Current Liabilities", "Current Assets", "Non-Current Assets", "P&L"]},
                    "operands": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "pattern": {"type": "string", "description": "pattern_id from the catalog"},
                                "op":      {"type": "string", "enum": ["+", "-", "*", "/"]},
                            },
                            "required": ["pattern", "op"],
                        },
                    },
                },
                "required": ["label", "section", "operands"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_rule",
            "description": "Remove a rule by label.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                },
                "required": ["label"],
            },
        },
    },
]


# ── Tool implementations ───────────────────────────────────────────

def _load_rules(ctx: ToolContext) -> Dict[str, Any]:
    return json.loads(ctx.rules_file.read_text(encoding="utf-8"))


def _save_rules(ctx: ToolContext, rules: Dict[str, Any]) -> None:
    # Validate every operand pattern against the catalog before persisting.
    patterns = json.loads(ctx.patterns_file.read_text(encoding="utf-8")).get("patterns", {})
    errors = []
    for label, rule in rules.items():
        if label.startswith("_"):
            continue
        ops = rule.get("operands") if isinstance(rule, dict) else None
        if not isinstance(ops, list):
            errors.append(f"Rule {label!r} has no operands list."); continue
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                errors.append(f"Rule {label!r} op[{i}] not an object."); continue
            pid = op.get("pattern"); o = op.get("op")
            if pid and pid not in patterns:
                errors.append(f"Rule {label!r} op[{i}] references unknown pattern {pid!r}.")
            if o not in ("+", "-", "*", "/"):
                errors.append(f"Rule {label!r} op[{i}].op invalid: {o!r}.")
    if errors:
        raise ValueError("; ".join(errors))
    # Backup before write
    try:
        shutil.copy(ctx.rules_file, ctx.rules_file.with_suffix(".backup.json"))
    except Exception:
        pass
    tmp = ctx.rules_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(rules, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    tmp.replace(ctx.rules_file)


def _tool_list_patterns(ctx: ToolContext, **_) -> Dict[str, Any]:
    out = []
    for entry in ctx.store.list_patterns_with_keywords():
        out.append({
            "pattern_id":   entry["pattern_id"],
            "name":         entry["name"],
            "group":        entry["group"],
            "section":      entry.get("section_filter"),
            "csv_source":   entry.get("csv_source"),
            "csv_category": entry.get("csv_category"),
            "n_csv_keywords": entry["n_csv"],
        })
    return {"ok": True, "patterns": out, "count": len(out)}


def _tool_find_patterns(ctx: ToolContext, query: str = "") -> Dict[str, Any]:
    q = (query or "").lower().strip()
    if not q:
        return {"ok": False, "error": "query is required"}
    tokens = [t for t in q.split() if t]
    matches = []
    for entry in ctx.store.list_patterns_with_keywords():
        hay = " ".join([
            entry["pattern_id"], entry["name"], entry["group"],
            entry.get("csv_category") or "", entry.get("section_filter") or "",
        ]).lower()
        if all(t in hay for t in tokens):
            matches.append({
                "pattern_id": entry["pattern_id"],
                "name":       entry["name"],
                "group":      entry["group"],
                "csv_source": entry.get("csv_source"),
                "csv_category": entry.get("csv_category"),
            })
    return {"ok": True, "matches": matches[:8], "count": len(matches)}


def _tool_list_keywords(ctx: ToolContext, pattern_id: str = "") -> Dict[str, Any]:
    spec = ctx.store.load_specs().get(pattern_id)
    if not spec:
        return {"ok": False, "error": f"Unknown pattern_id: {pattern_id!r}"}
    if not spec.csv_source or not spec.csv_category:
        return {"ok": True, "pattern_id": pattern_id, "csv_source": None,
                "csv_keywords": [], "extras": spec.extras,
                "note": "Hand-curated pattern -- no editable CSV bucket."}
    kws = ctx.store.list_keywords(spec.csv_source, spec.csv_category)
    # Truncate to avoid blowing the context
    return {"ok": True, "pattern_id": pattern_id, "csv_source": spec.csv_source,
            "csv_category": spec.csv_category, "csv_keywords_count": len(kws),
            "csv_keywords_sample": kws[-20:], "extras": spec.extras}


def _tool_add_keyword(ctx: ToolContext, pattern_id: str = "",
                       keyword: str = "") -> Dict[str, Any]:
    pid = (pattern_id or "").strip()
    kw  = (keyword or "").strip()
    if not pid: return {"ok": False, "error": "pattern_id is required"}
    if not kw:  return {"ok": False, "error": "keyword is required"}
    if len(kw) > 200:
        return {"ok": False, "error": "keyword too long (max 200 chars)"}
    spec = ctx.store.load_specs().get(pid)
    if not spec:
        return {"ok": False, "error": f"Unknown pattern_id: {pid!r}"}
    if not spec.csv_source or not spec.csv_category:
        return {"ok": False,
                "error": f"Pattern {pid!r} is hand-curated -- it has no CSV bucket. "
                         f"Edit data/pattern_specs.json `extras` and restart instead."}
    try:
        added = ctx.store.add_keyword(spec.csv_source, spec.csv_category, kw)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    ctx.store.build_patterns()
    return {"ok": True, "pattern_id": pid, "keyword": kw,
            "csv_source": spec.csv_source, "csv_category": spec.csv_category,
            "added": added,
            "note": "duplicate" if not added else None}


def _tool_delete_keyword(ctx: ToolContext, pattern_id: str = "",
                          keyword: str = "") -> Dict[str, Any]:
    pid = (pattern_id or "").strip()
    kw  = (keyword or "").strip()
    if not pid: return {"ok": False, "error": "pattern_id is required"}
    if not kw:  return {"ok": False, "error": "keyword is required"}
    spec = ctx.store.load_specs().get(pid)
    if not spec:
        return {"ok": False, "error": f"Unknown pattern_id: {pid!r}"}
    if not spec.csv_source or not spec.csv_category:
        return {"ok": False, "error": f"Pattern {pid!r} is hand-curated."}
    removed = ctx.store.delete_keyword(spec.csv_source, spec.csv_category, kw)
    ctx.store.build_patterns()
    return {"ok": True, "pattern_id": pid, "keyword": kw, "removed": removed}


def _tool_list_rules(ctx: ToolContext, **_) -> Dict[str, Any]:
    rules = _load_rules(ctx)
    out = []
    for label, rule in rules.items():
        if label.startswith("_"):
            continue
        out.append({
            "label":    label,
            "section":  rule.get("section"),
            "operands": rule.get("operands", []),
        })
    return {"ok": True, "rules": out, "count": len(out)}


def _tool_set_rule(ctx: ToolContext, label: str = "", section: str = "",
                    operands: Optional[List[Dict]] = None) -> Dict[str, Any]:
    label = (label or "").strip()
    if not label:    return {"ok": False, "error": "label is required"}
    if not section:  return {"ok": False, "error": "section is required"}
    if not operands: return {"ok": False, "error": "operands is required (at least 1)"}
    rules = _load_rules(ctx)
    existed = label in rules and not label.startswith("_")
    rules[label] = {"section": section, "operands": list(operands)}
    try:
        _save_rules(ctx, rules)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "label": label, "section": section,
            "operands_count": len(operands),
            "operation": "updated" if existed else "created"}


def _tool_delete_rule(ctx: ToolContext, label: str = "") -> Dict[str, Any]:
    label = (label or "").strip()
    if not label: return {"ok": False, "error": "label is required"}
    rules = _load_rules(ctx)
    if label not in rules:
        return {"ok": False, "error": f"No rule with label {label!r}"}
    del rules[label]
    try:
        _save_rules(ctx, rules)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "label": label, "deleted": True}


_TOOL_FNS: Dict[str, Callable] = {
    "list_patterns":   _tool_list_patterns,
    "find_patterns":   _tool_find_patterns,
    "list_keywords":   _tool_list_keywords,
    "add_keyword":     _tool_add_keyword,
    "delete_keyword":  _tool_delete_keyword,
    "list_rules":      _tool_list_rules,
    "set_rule":        _tool_set_rule,
    "delete_rule":     _tool_delete_rule,
}


# ── Agent ─────────────────────────────────────────────────────────

@dataclass
class Assistant:
    api_key: str
    model: str = "gpt-4o-mini"
    max_rounds: int = 6
    _client: Any = field(default=None, init=False)

    def __post_init__(self):
        if not (self.api_key or "").strip():
            raise ConfigurationError(
                "No OpenAI API key configured. Open /settings and paste one.")
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ConfigurationError("Run `pip install -r requirements.txt`.") from e
        self._client = OpenAI(api_key=self.api_key)

    def chat(self, user_messages: List[Dict[str, str]],
              ctx: ToolContext) -> Dict[str, Any]:
        """Run one /api/ai/chat turn. user_messages is the full history
        from the browser, ordered oldest-first, only role+content fields.
        Returns: {reply, actions, transcript}.
            reply     -- final assistant text for the UI
            actions   -- list of {tool, args, result} for the action log
            transcript -- the new message list to send back to the browser
                          (the user_messages plus the assistant's final
                          message; tool calls are NOT shown to user)
        """
        # Build the message list for OpenAI (with system prompt up front)
        sys = {"role": "system", "content": SYSTEM_PROMPT}
        messages_for_api: List[Dict[str, Any]] = [sys] + [
            {"role": m["role"], "content": m["content"]}
            for m in user_messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]

        actions: List[Dict[str, Any]] = []

        for round_i in range(self.max_rounds):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages_for_api,
                    tools=TOOL_SPECS,
                    tool_choice="auto",
                    temperature=0.0,
                )
            except Exception as e:
                raise AssistantError(f"OpenAI request failed: {e}") from e

            choice = response.choices[0]
            msg = choice.message
            assistant_entry: Dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
            }
            if msg.tool_calls:
                assistant_entry["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                   "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ]
            messages_for_api.append(assistant_entry)

            if not msg.tool_calls:
                # Final assistant reply
                return {
                    "reply":   msg.content or "",
                    "actions": actions,
                    "transcript": user_messages + [
                        {"role": "assistant", "content": msg.content or ""}
                    ],
                }

            # Execute each tool call and append a tool message
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                fn = _TOOL_FNS.get(tool_name)
                if not fn:
                    result = {"ok": False, "error": f"Unknown tool: {tool_name}"}
                else:
                    try:
                        result = fn(ctx, **tool_args)
                    except TypeError as e:
                        result = {"ok": False,
                                  "error": f"Bad arguments for {tool_name}: {e}"}
                    except Exception as e:
                        log.exception("Tool %s failed", tool_name)
                        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                actions.append({"tool": tool_name, "args": tool_args, "result": result})
                messages_for_api.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str)[:4000],
                })

        # Ran out of rounds
        return {
            "reply": ("I made several tool calls but didn't reach a final "
                       "answer. Try splitting your request into smaller steps."),
            "actions": actions,
            "transcript": user_messages + [
                {"role": "assistant", "content":
                    "I made several tool calls but didn't reach a final answer. "
                    "Try splitting your request into smaller steps."}
            ],
        }
