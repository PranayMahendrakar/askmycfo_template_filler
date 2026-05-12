"""
keyword_store.py — the single source of truth for the keyword dictionary.

Architecture
────────────
The user's keyword library lives in two CSV files (preserved verbatim):

    data/keywords/bs_keywords.csv   — 30 BS categories × ~200 variations
    data/keywords/pl_keywords.csv   — 11 PL categories × ~500 variations

These files are the canonical store. They are read on app start, mutated by
the keyword-editor UI (append on add, line-delete on remove), and never
filtered destructively — the user's data stays as-is.

A separate config file:

    data/pattern_specs.json

declares the runtime pattern catalog: each entry is one pattern_id that
maps to ONE CSV category (or none, for hand-curated-only patterns like
Deferred Tax Liabilities), plus a list of `extras` aliases hand-curated by
us, plus an optional section_filter for disambiguation.

At runtime, `build_patterns()` derives the runtime pattern dict by combining
each pattern's CSV variations + extras, applying a NON-DESTRUCTIVE alias
filter (drops single-word/tail-noise-only aliases that would cause spurious
substring matches — but only at *runtime*; the CSVs themselves are
untouched), and writes the result to data/patterns.json for the rule engine
to consume.

When the user adds or removes a keyword via /api/keywords, the CSV is
mutated and patterns.json is rebuilt. No other state needs to change.

Public API
──────────
    KeywordStore(data_dir)
        .list_categories(csv_source) -> list[str]
        .list_keywords(csv_source, category) -> list[str]
        .add_keyword(csv_source, category, keyword) -> bool
        .delete_keyword(csv_source, category, keyword) -> bool
        .build_patterns() -> dict   # also writes patterns.json
        .list_patterns_with_keywords() -> list of {pattern_id, name, group,
                                                    csv_source, csv_category,
                                                    extras, csv_keywords,
                                                    section_filter}
"""
from __future__ import annotations

import csv
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─── Constants ────────────────────────────────────────────────────────

# Words that on their own (or in combinations of only these) are tail noise
# from the user's CSV generation process. Filtering happens at *alias* time,
# not at storage time — the CSVs stay intact.
_NOISE_WORDS = frozenset({
    "the", "of", "and", "for", "in", "on", "at", "as", "a", "an",
    "year", "years", "march", "31", "fy",
    "consolidated", "standalone",
    "outstanding", "refer", "note", "notes", "per",
    "schedule", "balance", "amount", "ind", "gross",
    "net", "total", "value", "figure", "number",
    "current", "fiscal", "ended", "ending",
    "to", "from", "by", "with", "is", "are",
})

_PUNCT_RE = re.compile(r"[\s\-_/&,\.\(\)\[\]:;]+")


# ─── Helpers ──────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Lowercase + collapse punctuation/whitespace to single spaces."""
    s = (s or "").lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    return s.strip()


def _strip_category_prefix(variation: str, category: str) -> str:
    """If a CSV variation begins with the category name, drop that prefix.
    e.g. 'Share Capital equity share capital' (category 'Share Capital')
    -> 'equity share capital'. Returns the normalized form."""
    n_var = _normalize(variation)
    n_cat = _normalize(category)
    if n_var == n_cat:
        return n_cat
    if n_var.startswith(n_cat + " "):
        return n_var[len(n_cat) + 1:].strip()
    return n_var


def _is_noise_only(alias: str) -> bool:
    """True if the alias would cause spurious matches:
       - only noise words (e.g. 'for the year', 'consolidated')
       - only one substantive token, unless it's a long meaningful one
       - no token of ≥4 chars
    Used at build time to filter the runtime alias list, NOT to mutate the
    underlying CSV."""
    tokens = [t for t in alias.split() if t]
    if not tokens:
        return True
    substantive = [t for t in tokens
                    if t not in _NOISE_WORDS and not t.isdigit()]
    if not substantive:
        return True
    if not any(len(t) >= 4 for t in substantive):
        return True
    # Require ≥2 substantive tokens OR a single ≥10-char token (allows
    # 'goodwill', 'inventories' to stand alone; rejects bare 'equity').
    if len(substantive) < 2 and not any(len(t) >= 10 for t in substantive):
        return True
    return False


# ─── Store ────────────────────────────────────────────────────────────

@dataclass
class PatternSpec:
    """One entry in pattern_specs.json — declarative catalog row."""
    id: str
    name: str
    group: str
    section_filter: Optional[str]
    csv_source: Optional[str]     # 'bs' | 'pl' | None
    csv_category: Optional[str]   # bucket name in that CSV
    extras: List[str] = field(default_factory=list)


class KeywordStore:
    """Owns the keyword dictionary. Thread-safe for concurrent UI edits."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.bs_csv = self.data_dir / "keywords" / "bs_keywords.csv"
        self.pl_csv = self.data_dir / "keywords" / "pl_keywords.csv"
        self.specs_file = self.data_dir / "pattern_specs.json"
        self.patterns_out = self.data_dir / "patterns.json"
        self._lock = threading.Lock()

    # ── CSV path lookup ──
    def _csv_path(self, csv_source: str) -> Path:
        if csv_source == "bs":
            return self.bs_csv
        if csv_source == "pl":
            return self.pl_csv
        raise ValueError(f"Unknown csv_source: {csv_source!r}")

    # ── Specs ──
    def load_specs(self) -> Dict[str, PatternSpec]:
        """Parse pattern_specs.json into a dict of PatternSpec."""
        doc = json.loads(self.specs_file.read_text(encoding="utf-8"))
        out: Dict[str, PatternSpec] = {}
        for pid, p in doc.get("patterns", {}).items():
            out[pid] = PatternSpec(
                id=pid,
                name=p["name"],
                group=p["group"],
                section_filter=p.get("section_filter"),
                csv_source=p.get("csv_source"),
                csv_category=p.get("csv_category"),
                extras=p.get("extras") or [],
            )
        return out

    def load_groups(self) -> Dict[str, str]:
        doc = json.loads(self.specs_file.read_text(encoding="utf-8"))
        return doc.get("_groups", {})

    # ── CSV CRUD ──
    def _read_csv(self, csv_path: Path) -> List[Tuple[str, str]]:
        """Return [(field, variation), ...] preserving order. Missing file -> []."""
        if not csv_path.exists():
            return []
        rows: List[Tuple[str, str]] = []
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                f = (r.get("Field") or "").strip()
                v = (r.get("Keyword Variation") or "").strip()
                if f and v:
                    rows.append((f, v))
        return rows

    def _write_csv(self, csv_path: Path, rows: List[Tuple[str, str]]) -> None:
        """Atomic write. Backs up to .bak before replace."""
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        if csv_path.exists():
            csv_path.replace(csv_path.with_suffix(csv_path.suffix + ".bak"))
        tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Field", "Keyword Variation"])
            for f, v in rows:
                w.writerow([f, v])
        tmp.replace(csv_path)

    def list_categories(self, csv_source: str) -> List[str]:
        rows = self._read_csv(self._csv_path(csv_source))
        seen: Dict[str, None] = {}
        for f, _ in rows:
            seen[f] = None
        return list(seen.keys())

    def list_keywords(self, csv_source: str, category: str) -> List[str]:
        rows = self._read_csv(self._csv_path(csv_source))
        out: List[str] = []
        for f, v in rows:
            if f == category:
                out.append(v)
        return out

    def add_keyword(self, csv_source: str, category: str, keyword: str) -> bool:
        """Append (category, keyword) to the CSV. Returns True if added,
        False if duplicate (case-insensitive on the variation)."""
        keyword = (keyword or "").strip()
        if not keyword:
            raise ValueError("Empty keyword")
        if not category:
            raise ValueError("Empty category")

        with self._lock:
            path = self._csv_path(csv_source)
            rows = self._read_csv(path)
            existing_lower = {v.lower() for f, v in rows if f == category}
            if keyword.lower() in existing_lower:
                return False
            rows.append((category, keyword))
            self._write_csv(path, rows)
        return True

    def delete_keyword(self, csv_source: str, category: str, keyword: str) -> bool:
        """Remove the first matching (category, keyword) row. Case-insensitive
        match on variation. Returns True if removed."""
        with self._lock:
            path = self._csv_path(csv_source)
            rows = self._read_csv(path)
            kept: List[Tuple[str, str]] = []
            removed = False
            target = keyword.strip().lower()
            for f, v in rows:
                if not removed and f == category and v.strip().lower() == target:
                    removed = True
                    continue
                kept.append((f, v))
            if removed:
                self._write_csv(path, kept)
        return removed

    # ── Pattern build ──
    def _aliases_for_spec(self, spec: PatternSpec,
                          csv_index: Dict[str, Dict[str, List[str]]]
                          ) -> List[str]:
        """Combine CSV variations (under csv_category) + extras, normalize,
        filter noise, dedupe, sort longest-first."""
        aliases: set = set()
        # 1. CSV variations (with category prefix stripped, noise filtered)
        if spec.csv_source and spec.csv_category:
            variations = csv_index.get(spec.csv_source, {}).get(spec.csv_category, [])
            # Always include the category name itself as an alias
            aliases.add(_normalize(spec.csv_category))
            for v in variations:
                a = _strip_category_prefix(v, spec.csv_category)
                if len(a) < 4:
                    continue
                if not re.search(r"[a-z]{3,}", a):
                    continue
                if _is_noise_only(a):
                    continue
                aliases.add(a)
        # 2. Hand-curated extras — these bypass the noise filter (we vouch
        #    for them) but still get normalized.
        for e in spec.extras:
            ne = _normalize(e)
            if ne:
                aliases.add(ne)
        # Stable order: longest first (cosmetic + matches longest-alias-wins)
        return sorted(aliases, key=lambda x: (-len(x), x))

    def _build_csv_index(self) -> Dict[str, Dict[str, List[str]]]:
        """Pre-read both CSVs into {csv_source: {category: [variations]}}."""
        idx: Dict[str, Dict[str, List[str]]] = {"bs": {}, "pl": {}}
        for src, path in (("bs", self.bs_csv), ("pl", self.pl_csv)):
            for f, v in self._read_csv(path):
                idx[src].setdefault(f, []).append(v)
        return idx

    def build_patterns(self) -> Dict[str, Any]:
        """Rebuild patterns.json from CSVs + specs. Writes to disk and
        returns the in-memory dict."""
        with self._lock:
            specs = self.load_specs()
            groups = self.load_groups()
            csv_index = self._build_csv_index()

            patterns: Dict[str, Any] = {}
            for pid, spec in specs.items():
                patterns[pid] = {
                    "name": spec.name,
                    "group": spec.group,
                    "section_filter": spec.section_filter,
                    "csv_source": spec.csv_source,
                    "csv_category": spec.csv_category,
                    "aliases": self._aliases_for_spec(spec, csv_index),
                }
            doc = {
                "_comment": (
                    "AUTO-GENERATED by KeywordStore.build_patterns(). Aliases "
                    "are derived from data/keywords/{bs,pl}_keywords.csv + "
                    "extras in data/pattern_specs.json. Do not edit this file "
                    "directly -- edit the CSVs (via the /keywords UI) or "
                    "pattern_specs.json (for new patterns) and the next "
                    "server start (or any keyword save) regenerates this."
                ),
                "_groups": groups,
                "patterns": patterns,
            }
            tmp = self.patterns_out.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(doc, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.patterns_out)
        return doc

    def list_patterns_with_keywords(self) -> List[Dict[str, Any]]:
        """For the /keywords UI. Returns one dict per pattern containing the
        pattern metadata AND the raw list of CSV variations under its CSV
        category (so the UI can show, add, remove them). Hand-curated
        `extras` are returned read-only (they live in pattern_specs.json,
        not in the CSV, so they aren't editable from the keywords UI)."""
        specs = self.load_specs()
        csv_index = self._build_csv_index()
        out: List[Dict[str, Any]] = []
        for pid, spec in specs.items():
            csv_keywords: List[str] = []
            if spec.csv_source and spec.csv_category:
                csv_keywords = list(csv_index.get(spec.csv_source, {})
                                              .get(spec.csv_category, []))
            out.append({
                "pattern_id": pid,
                "name": spec.name,
                "group": spec.group,
                "section_filter": spec.section_filter,
                "csv_source": spec.csv_source,
                "csv_category": spec.csv_category,
                "extras": list(spec.extras),
                "csv_keywords": csv_keywords,
                "n_csv": len(csv_keywords),
                "n_extras": len(spec.extras),
            })
        return out


# ─── CLI ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    here = Path(__file__).parent.parent
    store = KeywordStore(here / "data")
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        doc = store.build_patterns()
        n = len(doc["patterns"])
        n_aliases = sum(len(p["aliases"]) for p in doc["patterns"].values())
        print(f"wrote {store.patterns_out}: {n} patterns, {n_aliases} aliases")
    elif len(sys.argv) > 1 and sys.argv[1] == "list":
        for entry in store.list_patterns_with_keywords():
            print(f"  {entry['pattern_id']:42s}  "
                  f"csv={entry['n_csv']:>4d}  extras={entry['n_extras']:>3d}")
    else:
        print("Usage:")
        print("  python -m modules.keyword_store build")
        print("  python -m modules.keyword_store list")
