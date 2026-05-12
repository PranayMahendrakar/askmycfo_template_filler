"""
ai_classifier.py — uses an OpenAI model to map unmatched line-item labels
to the correct pattern_id in our catalog.

Why
───
After the rule engine runs, some rows in the extracted Excel may not match
any pattern — usually because the report uses a phrasing the keyword
dictionary doesn't have yet. Sending those labels to an LLM with the
catalog as context recovers most of them. The classified labels are then
appended to the user's CSV dictionary so the same issue never recurs.

Design
──────
- One batched call per pipeline run (cheap, fast). All unmatched labels go
  in one prompt.
- Strict JSON output with `pattern_id`, `confidence` (0-1), and a short
  `reasoning` field.
- Model is configurable — default `gpt-4o-mini`, but the user can pick any
  chat-completions model from the settings page.
- The classifier knows nothing about CSVs or rule engines. It's a pure
  function (labels + catalog) -> classifications. Wiring lives in app.py.

Failure modes are explicit:
  - missing/empty API key -> raises ConfigurationError
  - network/auth failure  -> raises ClassifierError with the upstream message
  - malformed JSON        -> raises ClassifierError("model returned non-JSON")
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


log = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when the classifier can't be constructed (no key, etc.)."""


class ClassifierError(Exception):
    """Raised when the model call itself fails."""


@dataclass
class Classification:
    label: str
    pattern_id: Optional[str]
    confidence: float
    reasoning: str
    section_hint: Optional[str] = None  # set by upstream if known

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "pattern_id": self.pattern_id,
            "confidence": round(float(self.confidence), 3),
            "reasoning": self.reasoning,
            "section_hint": self.section_hint,
        }


@dataclass
class AIClassifier:
    api_key: str
    model: str = "gpt-4o-mini"

    def __post_init__(self):
        if not self.api_key or not self.api_key.strip():
            raise ConfigurationError(
                "No OpenAI API key configured. Open /settings and paste one.")
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ConfigurationError(
                "The `openai` package isn't installed. Run "
                "`pip install -r requirements.txt`.") from e
        self._client = OpenAI(api_key=self.api_key)

    # ── Catalog formatting (kept small to stay under context limits) ──

    def _format_catalog(self, patterns: Dict[str, Dict]) -> str:
        """Render the pattern catalog as a compact reference the model can
        scan. We use the pattern name + a few representative aliases per
        entry — enough signal, low token cost."""
        lines: List[str] = []
        groups: Dict[str, List[str]] = {}
        for pid, p in patterns.items():
            groups.setdefault(p.get("group", "other"), []).append(pid)

        group_order = ["equity", "current_liab", "noncurrent_liab",
                       "current_asset", "noncurrent_asset", "pl"]
        group_titles = {
            "equity": "EQUITY / NETWORTH",
            "current_liab": "CURRENT LIABILITIES",
            "noncurrent_liab": "NON-CURRENT LIABILITIES",
            "current_asset": "CURRENT ASSETS",
            "noncurrent_asset": "NON-CURRENT ASSETS",
            "pl": "PROFIT & LOSS",
        }
        for g in [*group_order, *[k for k in groups if k not in group_order]]:
            if g not in groups:
                continue
            lines.append(f"\n### {group_titles.get(g, g.upper())}")
            for pid in groups[g]:
                p = patterns[pid]
                # Take 4 short, distinctive aliases (skip the noisy ones)
                aliases = [a for a in p.get("aliases", [])
                            if 6 <= len(a) <= 50][:4]
                ex = ("; ".join(aliases)) if aliases else "(no examples)"
                lines.append(f"  - {pid}: {p['name']}")
                lines.append(f"      examples: {ex}")
        return "\n".join(lines)

    # ── Public API ──

    def classify_labels(self,
                        labels: List[str],
                        patterns: Dict[str, Dict],
                        section_hints: Optional[Dict[str, str]] = None
                        ) -> List[Classification]:
        """Classify each label in one batched call.
        Returns a list of Classification, same length and order as `labels`.
        Unmatched -> pattern_id=None, confidence=0."""
        if not labels:
            return []

        section_hints = section_hints or {}
        labels_block: List[str] = []
        for i, lbl in enumerate(labels, 1):
            hint = section_hints.get(lbl)
            tail = f"  (section: {hint})" if hint else ""
            labels_block.append(f"{i}. {lbl}{tail}")
        labels_str = "\n".join(labels_block)

        catalog_str = self._format_catalog(patterns)

        system = (
            "You are a senior Indian Chartered Accountant. Your job is to "
            "classify line items from an annual report's Balance Sheet and "
            "Profit & Loss statement into a catalog of standard patterns. "
            "Schedule III of the Companies Act and Ind AS conventions apply."
        )

        user = (
            f"PATTERN CATALOG (one pattern_id per line):\n{catalog_str}\n\n"
            f"LABELS TO CLASSIFY:\n{labels_str}\n\n"
            "For each label, return the single best pattern_id it belongs "
            "to. Use the section hint (in parentheses, if present) to "
            "disambiguate between current/non-current variants. If a label "
            "is a sub-total, page header, or otherwise has no meaningful "
            "pattern, set pattern_id to null.\n\n"
            "Respond with strict JSON in this shape:\n"
            '{ "classifications": [ '
            '{ "label": "...", "pattern_id": "...", '
            '"confidence": 0.0, "reasoning": "..." }, '
            '... ] }\n'
            "- confidence must be between 0.0 and 1.0\n"
            "- reasoning must be one short sentence (<= 20 words)\n"
            "- preserve the label exactly as given (including case)\n"
            f"- return exactly {len(labels)} classifications, one per input"
        )

        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
        except Exception as e:
            raise ClassifierError(f"OpenAI request failed: {e}") from e

        text = resp.choices[0].message.content or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ClassifierError(
                f"Model returned non-JSON output: {text[:200]!r}") from e

        # Accept either {"classifications":[...]} or a bare list under any key
        items: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            if isinstance(data.get("classifications"), list):
                items = data["classifications"]
            else:
                # Single-key dict with a list value
                for v in data.values():
                    if isinstance(v, list):
                        items = v
                        break
        elif isinstance(data, list):
            items = data

        # Index by label (case-insensitive) so we can produce a deterministic
        # output array even if the model reorders or drops entries
        by_label = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            lbl = it.get("label") or ""
            by_label[lbl.strip().lower()] = it

        out: List[Classification] = []
        valid_pids = set(patterns.keys())
        for lbl in labels:
            it = by_label.get(lbl.strip().lower(), {})
            pid = it.get("pattern_id")
            if pid not in valid_pids:
                pid = None
            try:
                conf = float(it.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            conf = max(0.0, min(1.0, conf))
            if pid is None:
                conf = 0.0
            out.append(Classification(
                label=lbl,
                pattern_id=pid,
                confidence=conf,
                reasoning=(it.get("reasoning") or "")[:240],
                section_hint=section_hints.get(lbl),
            ))
        return out

    # ── Diagnostic ──

    def health_check(self) -> Dict[str, Any]:
        """Make a tiny request to validate the key+model. Returns a small
        dict describing what worked."""
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "Reply with: pong"}],
                max_tokens=10,
                temperature=0.0,
            )
        except Exception as e:
            raise ClassifierError(f"OpenAI request failed: {e}") from e
        content = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        return {
            "ok": True,
            "model": self.model,
            "reply": content,
            "tokens_used": getattr(usage, "total_tokens", None) if usage else None,
        }


# ── Supported models (UI dropdown) ─────────────────────────────────────

SUPPORTED_MODELS = [
    # (id,                       label,                            notes)
    ("gpt-4o-mini",              "GPT-4o mini",                    "fast & cheap, recommended for batch classification"),
    ("gpt-4o",                   "GPT-4o",                         "stronger reasoning, ~10x cost"),
    ("gpt-4-turbo",              "GPT-4 Turbo",                    "older but reliable"),
    ("gpt-4.1",                  "GPT-4.1",                        "latest reasoning model (if available on your plan)"),
    ("gpt-4.1-mini",             "GPT-4.1 mini",                   "balanced cost/quality"),
    ("gpt-3.5-turbo",            "GPT-3.5 Turbo",                  "cheapest, weakest"),
]
