"""
app.py -- AskMyCFO Simple Template Filler.

Pages:
    /            upload PDF, run pipeline
    /rules       formula editor
    /keywords    keyword dictionary editor
    /settings    OpenAI API key + model picker
    /result/<j>  result + downloads + AI auto-fix button

APIs:
    GET/POST /api/rules           -- rule set CRUD
    POST     /api/rules/reset     -- restore defaults
    GET      /api/patterns        -- runtime catalog
    GET      /api/cell_map        -- template label -> row map

    GET      /api/keywords        -- patterns + their CSV keywords + extras
    POST     /api/keywords/add    -- append keyword to CSV bucket
    POST     /api/keywords/delete -- remove keyword from CSV bucket

    GET/POST /api/settings        -- OpenAI api_key, model, auto_apply_threshold
    POST     /api/settings/test   -- ping OpenAI to validate api_key + model
    POST     /api/ai/auto_fix     -- run AI on unmatched + add to CSV + re-run

Run:
    python3 app.py    ->  http://127.0.0.1:5005
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import (Flask, jsonify, render_template, request, send_file,
                   redirect, url_for, flash)
from werkzeug.utils import secure_filename

from modules.page_detector import detect_pages, extract_pages
from modules.extract_tables import extract_tables
from modules.rule_engine import run as run_rules
from modules.template_writer import write_filled_template, try_recalc
from modules.keyword_store import KeywordStore
from modules.ai_classifier import (AIClassifier, SUPPORTED_MODELS,
                                    ConfigurationError, ClassifierError)
from modules.ai_assistant import (Assistant, ToolContext,
                                   ConfigurationError as AsstConfigError,
                                   AssistantError)


# ─── Paths ──────────────────────────────────────────────────────────

BASE       = Path(__file__).parent.resolve()
DATA_DIR   = BASE / "data"
UPLOAD_DIR = BASE / "uploads"
OUTPUT_DIR = BASE / "outputs"
TEMPLATE   = BASE / "Input_Templates.xlsx"
RULES_FILE    = DATA_DIR / "rules.json"
PATTERNS_FILE = DATA_DIR / "patterns.json"
CELLMAP_FILE  = DATA_DIR / "cell_map.json"
AI_CONFIG     = DATA_DIR / "ai_config.json"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

STORE = KeywordStore(DATA_DIR)


# ─── App ────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB PDF cap

# Cache-busting for static files. Every server start gets a fresh version
# string so the browser can't serve a stale JS file from cache. Templates
# refer to it as {{ static_v }}.
_STATIC_VERSION = str(int(time.time()))


@app.context_processor
def _inject_static_version():
    return {"static_v": _STATIC_VERSION}


# ─── Helpers: JSON I/O (always utf-8) ───────────────────────────────

def _load_json(p: Path) -> Dict[str, Any]:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(p: Path, data: Dict[str, Any]) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    tmp.replace(p)


def _safe_stem(name: str) -> str:
    name = re.sub(r"[^\w\-]+", "_", (name or "").strip())
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "company"


def _api_err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


# ─── Units ──────────────────────────────────────────────────────────

# extract_tables.py normalises all values to LAKHS. These factors convert
# lakhs → the target unit. (1 crore = 100 lakhs, 1 lakh = 100,000 actuals.)
UNIT_FACTORS = {
    "lakhs":     {"factor": 1.0,       "label": "₹ Lakhs",    "suffix": "Lakhs"},
    "crores":    {"factor": 0.01,      "label": "₹ Crores",   "suffix": "Crores"},
    "millions":  {"factor": 0.1,       "label": "₹ Millions", "suffix": "Millions"},
    "actual":    {"factor": 100000.0,  "label": "₹ Actual",   "suffix": "INR (Actual)"},
}


def _apply_units(values: Dict[str, Dict[str, float]], unit: str
                 ) -> Dict[str, Dict[str, float]]:
    """Scale every cur/pri in the values dict by the unit's factor.
    Returns a new dict, leaves trace/sources untouched."""
    factor = UNIT_FACTORS.get(unit, UNIT_FACTORS["lakhs"])["factor"]
    if factor == 1.0:
        return values
    out: Dict[str, Dict[str, float]] = {}
    for k, v in values.items():
        cur = (v.get("cur") or 0) * factor
        pri = (v.get("pri") or 0) * factor
        out[k] = {**v, "cur": cur, "pri": pri}
    return out


# ─── AI config ──────────────────────────────────────────────────────

def _load_ai_config() -> Dict[str, Any]:
    if AI_CONFIG.exists():
        try:
            return _load_json(AI_CONFIG)
        except Exception:
            pass
    return {
        "openai_api_key": "",
        "model": "gpt-4o-mini",
        "auto_apply_threshold": 0.7,
    }


def _mask_api_key(k: str) -> str:
    if not k:
        return ""
    k = k.strip()
    if len(k) <= 10:
        return "*" * len(k)
    return k[:4] + "*" * (len(k) - 8) + k[-4:]


def _resolve_api_key(cfg: Dict[str, Any]) -> str:
    """API key precedence: env var > config file. So the user can override
    on a deploy without editing the file."""
    return os.environ.get("OPENAI_API_KEY", "").strip() \
           or (cfg.get("openai_api_key") or "").strip()


# ─── Routes: HTML pages ─────────────────────────────────────────────

@app.route("/")
def home():
    cell_map = _load_json(CELLMAP_FILE)
    rules = _load_json(RULES_FILE)
    n_rules = len([k for k in rules if not k.startswith("_")])
    cfg = _load_ai_config()
    return render_template("index.html",
                           n_rules=n_rules,
                           n_rows=len(cell_map.get("data_rows", {})),
                           ai_ready=bool(_resolve_api_key(cfg)),
                           unit_options=[
                               ("lakhs", "₹ Lakhs"),
                               ("crores", "₹ Crores"),
                               ("millions", "₹ Millions"),
                               ("actual", "₹ Actual (raw INR)"),
                           ])


@app.route("/rules")
def rules_page():
    return render_template("rules.html")


@app.route("/keywords")
def keywords_page():
    return render_template("keywords.html")


@app.route("/settings")
def settings_page():
    cfg = _load_ai_config()
    return render_template("settings.html",
                           api_key_masked=_mask_api_key(cfg.get("openai_api_key", "")),
                           api_key_from_env=bool(os.environ.get("OPENAI_API_KEY")),
                           model=cfg.get("model", "gpt-4o-mini"),
                           threshold=cfg.get("auto_apply_threshold", 0.7),
                           supported_models=SUPPORTED_MODELS)


@app.route("/process", methods=["POST"])
def process():
    if "pdf" not in request.files:
        flash("No PDF uploaded.", "error")
        return redirect(url_for("home"))
    f = request.files["pdf"]
    if not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("home"))

    company       = request.form.get("company", "").strip()
    fy_label      = request.form.get("fy_label", "").strip()
    sheet_pref    = (request.form.get("sheet_preference") or "standalone").lower()
    units         = (request.form.get("units") or "lakhs").lower()
    auto_ai       = bool(request.form.get("auto_ai"))

    if sheet_pref not in ("standalone", "consolidated", "auto"):
        sheet_pref = "standalone"
    if units not in UNIT_FACTORS:
        units = "lakhs"

    safe_name = _safe_stem(company or Path(f.filename).stem)
    ts = time.strftime("%Y%m%d_%H%M%S")
    job_dir = OUTPUT_DIR / f"{safe_name}_{ts}"
    job_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = UPLOAD_DIR / f"{safe_name}_{ts}_{secure_filename(f.filename)}"
    f.save(pdf_path)
    app.logger.info(f"saved upload to {pdf_path}")

    result: Dict[str, Any] = {
        "company":          company or safe_name,
        "fy_label":         fy_label,
        "sheet_preference": sheet_pref,
        "units":            units,
        "units_label":      UNIT_FACTORS[units]["label"],
        "pdf":              str(pdf_path),
        "trimmed_pdf":      None,
        "extracted_xlsx":   None,
        "filled_xlsx":      None,
        "sections":         [],
        "values":           {},
        "unmatched":        [],
        "ai_runs":          [],   # populated by /api/ai/auto_fix
        "error":            None,
    }

    try:
        # 1. Detect BS/PL pages
        sections = detect_pages(str(pdf_path))
        result["sections"] = [
            {"type": s.section_type, "variant": s.variant,
             "page": s.page_number, "page_end": s.page_end,
             "score": round(s.score, 1), "markers": s.markers_found[:6]}
            for s in sections if s.section_type != "unknown"
        ]
        if not result["sections"]:
            raise RuntimeError("No Balance Sheet or P&L pages detected.")

        # 2. Trim PDF to those pages
        trimmed = extract_pages(str(pdf_path), output_dir=str(job_dir))
        if not trimmed or not Path(trimmed).exists():
            raise RuntimeError("Failed to extract BS/PL pages.")
        result["trimmed_pdf"] = trimmed

        # 3. Extract tables as-is
        extract_tables(trimmed, output_dir=str(job_dir))
        ex = list(Path(job_dir).glob("*_extracted.xlsx"))
        if not ex:
            raise RuntimeError("Table extraction produced no Excel file.")
        result["extracted_xlsx"] = str(ex[0])

        # 4. Apply rules with the chosen sheet preference
        rr = _apply_rules_and_write(
            extracted_xlsx=result["extracted_xlsx"],
            sheet_preference=sheet_pref,
            units=units,
            company=result["company"],
            fy_label=fy_label,
            out_xlsx=job_dir / f"{safe_name}_filled_template.xlsx",
        )
        result["values"]       = rr["values_display"]
        result["values_raw"]   = rr["values_raw"]
        result["unmatched"]    = rr["unmatched"]
        result["filled_xlsx"]  = rr["filled_xlsx"]

    except Exception as e:
        traceback.print_exc()
        result["error"] = f"{type(e).__name__}: {e}"

    res_path = job_dir / "_result.json"
    res_path.write_text(json.dumps(result, indent=2, default=str),
                        encoding="utf-8")

    # Optional: auto-run AI fix if requested AND key is configured AND we
    # have unmatched rows. The user can also click the button manually.
    if (auto_ai and not result["error"]
            and result["unmatched"]
            and _resolve_api_key(_load_ai_config())):
        try:
            _do_auto_fix(job_dir.name)
        except Exception as e:
            app.logger.error(f"auto-AI failed: {e}")

    return redirect(url_for("result", job=job_dir.name))


def _apply_rules_and_write(extracted_xlsx: str,
                            sheet_preference: str,
                            units: str,
                            company: str,
                            fy_label: str,
                            out_xlsx: Path) -> Dict[str, Any]:
    """Run rule_engine, apply units, write the filled template. Returns the
    display-ready values + raw lakhs values + unmatched + path."""
    rr = run_rules(extracted_xlsx, RULES_FILE, PATTERNS_FILE,
                    sheet_preference=sheet_preference)
    values_raw = rr["values"]                       # always in LAKHS
    values_display = _apply_units(values_raw, units)

    cell_map = _load_json(CELLMAP_FILE)
    write_filled_template(TEMPLATE, out_xlsx, values_display, cell_map,
                          company_name=company, fy_label=fy_label)
    try_recalc(out_xlsx)

    unmatched = [
        {"label": r["label"], "cur": r["cur"], "pri": r["pri"],
         "section": r.get("section"), "sheet_kind": r.get("sheet_kind")}
        for r in rr["unmatched"][:80]
    ]
    return {
        "values_display": values_display,
        "values_raw":     values_raw,
        "unmatched":      unmatched,
        "filled_xlsx":    str(out_xlsx),
    }


@app.route("/result/<job>")
def result(job: str):
    job_dir = OUTPUT_DIR / job
    res_path = job_dir / "_result.json"
    if not res_path.exists():
        flash("Job not found.", "error")
        return redirect(url_for("home"))
    result = json.loads(res_path.read_text(encoding="utf-8"))
    cell_map = _load_json(CELLMAP_FILE)
    cfg = _load_ai_config()
    return render_template("result.html",
                           job=job, result=result, cell_map=cell_map,
                           ai_ready=bool(_resolve_api_key(cfg)),
                           ai_model=cfg.get("model", "gpt-4o-mini"))


@app.route("/download/<job>/<which>")
def download(job: str, which: str):
    job_dir = OUTPUT_DIR / job
    if not job_dir.exists():
        return "Not found", 404
    res = json.loads((job_dir / "_result.json").read_text(encoding="utf-8"))
    if which == "filled" and res.get("filled_xlsx"):
        return send_file(res["filled_xlsx"], as_attachment=True)
    if which == "extracted" and res.get("extracted_xlsx"):
        return send_file(res["extracted_xlsx"], as_attachment=True)
    if which == "trimmed" and res.get("trimmed_pdf"):
        return send_file(res["trimmed_pdf"], as_attachment=True)
    return "Unknown download key", 400


# ─── API: rules + patterns ──────────────────────────────────────────

@app.get("/api/patterns")
def api_patterns():
    return jsonify(_load_json(PATTERNS_FILE))


@app.get("/api/cell_map")
def api_cell_map():
    return jsonify(_load_json(CELLMAP_FILE))


@app.get("/api/rules")
def api_rules_get():
    return jsonify(_load_json(RULES_FILE))


@app.post("/api/rules")
def api_rules_save():
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return _api_err("Body must be a JSON object.")
    patterns = _load_json(PATTERNS_FILE).get("patterns", {})
    errors = []
    for label, rule in body.items():
        if label.startswith("_"):
            continue
        if not isinstance(rule, dict):
            errors.append(f"Rule '{label}' is not an object."); continue
        operands = rule.get("operands") or []
        if not isinstance(operands, list):
            errors.append(f"Rule '{label}'.operands is not a list."); continue
        for i, op in enumerate(operands):
            if not isinstance(op, dict):
                errors.append(f"Rule '{label}'.operands[{i}] not an object."); continue
            pid = op.get("pattern")
            o = op.get("op")
            if pid and pid not in patterns:
                errors.append(f"Rule '{label}'.operands[{i}].pattern "
                              f"'{pid}' not in catalog.")
            if o not in ("+", "-", "*", "/"):
                errors.append(f"Rule '{label}'.operands[{i}].op must be "
                              f"+ / - / * / / (got {o!r}).")
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    if RULES_FILE.exists():
        try: shutil.copy(RULES_FILE, DATA_DIR / "rules.backup.json")
        except Exception: pass
    _save_json(RULES_FILE, body)
    return jsonify({"ok": True, "saved": True,
                    "n_rules": len([k for k in body if not k.startswith("_")])})


@app.post("/api/rules/reset")
def api_rules_reset():
    default = DATA_DIR / "rules.default.json"
    if not default.exists():
        return _api_err("No default snapshot available.", 404)
    shutil.copy(default, RULES_FILE)
    return jsonify({"ok": True, "restored": True})


# ─── API: keywords ──────────────────────────────────────────────────

@app.get("/api/keywords")
def api_keywords_list():
    entries = STORE.list_patterns_with_keywords()
    groups = STORE.load_groups()
    return jsonify({"patterns": entries, "groups": groups})


@app.post("/api/keywords/add")
def api_keywords_add():
    body = request.get_json(force=True, silent=True) or {}
    pid = (body.get("pattern_id") or "").strip()
    kw  = (body.get("keyword") or "").strip()
    if not pid: return _api_err("pattern_id is required.")
    if not kw:  return _api_err("keyword is required.")
    if len(kw) > 200: return _api_err("keyword is too long (max 200 chars).")
    specs = STORE.load_specs()
    spec = specs.get(pid)
    if not spec: return _api_err(f"Unknown pattern_id: {pid!r}", 404)
    if not spec.csv_source or not spec.csv_category:
        return _api_err(f"Pattern '{pid}' has no CSV source -- it's "
                        f"hand-curated. To add to it, edit "
                        f"data/pattern_specs.json `extras` and restart.", 409)
    try:
        added = STORE.add_keyword(spec.csv_source, spec.csv_category, kw)
    except Exception as e:
        return _api_err(str(e), 400)
    STORE.build_patterns()
    return jsonify({"ok": True, "added": added, "pattern_id": pid, "keyword": kw})


@app.post("/api/keywords/delete")
def api_keywords_delete():
    body = request.get_json(force=True, silent=True) or {}
    pid = (body.get("pattern_id") or "").strip()
    kw  = (body.get("keyword") or "").strip()
    if not pid: return _api_err("pattern_id is required.")
    if not kw:  return _api_err("keyword is required.")
    specs = STORE.load_specs()
    spec = specs.get(pid)
    if not spec: return _api_err(f"Unknown pattern_id: {pid!r}", 404)
    if not spec.csv_source or not spec.csv_category:
        return _api_err(f"Pattern '{pid}' has no CSV source.", 409)
    removed = STORE.delete_keyword(spec.csv_source, spec.csv_category, kw)
    STORE.build_patterns()
    return jsonify({"ok": True, "removed": removed,
                    "pattern_id": pid, "keyword": kw})


# ─── API: settings ──────────────────────────────────────────────────

@app.get("/api/settings")
def api_settings_get():
    cfg = _load_ai_config()
    return jsonify({
        "api_key_masked": _mask_api_key(cfg.get("openai_api_key", "")),
        "api_key_from_env": bool(os.environ.get("OPENAI_API_KEY")),
        "model": cfg.get("model", "gpt-4o-mini"),
        "auto_apply_threshold": cfg.get("auto_apply_threshold", 0.7),
        "supported_models": [
            {"id": m[0], "label": m[1], "notes": m[2]}
            for m in SUPPORTED_MODELS
        ],
    })


@app.post("/api/settings")
def api_settings_save():
    body = request.get_json(force=True, silent=True) or {}
    cfg = _load_ai_config()
    # Only overwrite api_key if a non-empty value was sent (so partial saves
    # don't wipe the existing key — the UI sends "" when the user didn't
    # touch the input).
    new_key = (body.get("api_key") or "").strip()
    if new_key:
        cfg["openai_api_key"] = new_key
    if "model" in body:
        m = (body.get("model") or "").strip()
        valid = {x[0] for x in SUPPORTED_MODELS}
        if m and m in valid:
            cfg["model"] = m
    if "auto_apply_threshold" in body:
        try:
            t = float(body["auto_apply_threshold"])
            cfg["auto_apply_threshold"] = max(0.0, min(1.0, t))
        except (TypeError, ValueError):
            pass
    _save_json(AI_CONFIG, cfg)
    return jsonify({
        "ok": True,
        "api_key_masked": _mask_api_key(cfg.get("openai_api_key", "")),
        "model": cfg["model"],
        "auto_apply_threshold": cfg["auto_apply_threshold"],
    })


@app.post("/api/settings/test")
def api_settings_test():
    """Ping the configured OpenAI model — useful 'is my key working?' check."""
    cfg = _load_ai_config()
    key = _resolve_api_key(cfg)
    if not key:
        return _api_err("No API key configured (env or settings).", 400)
    try:
        clf = AIClassifier(key, model=cfg.get("model", "gpt-4o-mini"))
        result = clf.health_check()
        return jsonify(result)
    except (ConfigurationError, ClassifierError) as e:
        return _api_err(str(e), 400)


# ─── API: AI auto-fix ───────────────────────────────────────────────

def _apply_label_mappings(job: str, mappings: List[Dict[str, str]],
                           source_tag: str) -> Dict[str, Any]:
    """Shared helper used by /api/ai/auto_fix and /api/manual_fix.

    Given a list of {label, pattern_id} pairs, append each label to the
    appropriate CSV bucket, rebuild patterns.json, re-run the rule engine
    on the job's extracted xlsx, re-write the filled template, and persist
    everything to result.json with a manual_fix or ai run record.

    Returns a dict the API hands back to the UI -- including before/after
    counts and the list of labels that were resolved by this re-run, so
    the user can see exactly what changed.
    """
    job_dir = OUTPUT_DIR / job
    res_path = job_dir / "_result.json"
    if not res_path.exists():
        raise RuntimeError(f"Job {job!r} not found.")
    result = json.loads(res_path.read_text(encoding="utf-8"))
    if result.get("error"):
        raise RuntimeError(f"Job failed earlier: {result['error']}")

    # ── Snapshot BEFORE state ───────────────────────────────
    before_unmatched_labels = {(u.get("label") or "").strip()
                                for u in (result.get("unmatched") or [])}
    before_count = len(before_unmatched_labels)
    before_values = dict(result.get("values") or {})

    app.logger.info(f"[{source_tag}_fix] job={job} mappings={len(mappings)} "
                     f"before_unmatched={before_count}")

    specs = STORE.load_specs()
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for m in mappings:
        label = (m.get("label") or "").strip()
        pid   = (m.get("pattern_id") or "").strip()
        if not label or not pid:
            skipped.append({"label": label, "pattern_id": pid,
                             "reason": "empty label or pattern_id"})
            continue
        spec = specs.get(pid)
        if not spec:
            skipped.append({"label": label, "pattern_id": pid,
                             "reason": f"unknown pattern_id {pid!r}"})
            continue
        if not spec.csv_source or not spec.csv_category:
            skipped.append({"label": label, "pattern_id": pid,
                             "reason": ("pattern is hand-curated -- no CSV "
                                          "bucket to append to")})
            continue
        try:
            added = STORE.add_keyword(spec.csv_source, spec.csv_category, label)
        except Exception as e:
            skipped.append({"label": label, "pattern_id": pid, "reason": str(e)})
            continue
        applied.append({
            "label": label, "pattern_id": pid,
            "csv_source": spec.csv_source, "csv_category": spec.csv_category,
            "added_to_csv": added,
            "duplicate": (not added),
        })
        app.logger.info(f"[{source_tag}_fix]   + {label!r} -> {pid} "
                         f"({spec.csv_source}.{spec.csv_category}) "
                         f"{'NEW' if added else 'duplicate'}")

    # ── Apply: re-run the pipeline ─────────────────────────
    resolved_labels: List[str] = []
    after_count = before_count
    value_diffs: List[Dict[str, Any]] = []

    if applied:
        STORE.build_patterns()
        rr = _apply_rules_and_write(
            extracted_xlsx=result["extracted_xlsx"],
            sheet_preference=result.get("sheet_preference", "standalone"),
            units=result.get("units", "lakhs"),
            company=result["company"],
            fy_label=result.get("fy_label", ""),
            out_xlsx=Path(result["filled_xlsx"]),
        )
        result["values"]       = rr["values_display"]
        result["values_raw"]   = rr["values_raw"]
        result["unmatched"]    = rr["unmatched"]
        result["filled_xlsx"]  = rr["filled_xlsx"]

        after_unmatched_labels = {(u.get("label") or "").strip()
                                   for u in rr["unmatched"]}
        after_count = len(after_unmatched_labels)
        resolved_labels = sorted(before_unmatched_labels - after_unmatched_labels)

        # Compute value diffs so the user can see what changed in the template
        for label, after_v in (result.get("values") or {}).items():
            before_v = before_values.get(label, {"cur": 0, "pri": 0})
            d_cur = (after_v.get("cur") or 0) - (before_v.get("cur") or 0)
            d_pri = (after_v.get("pri") or 0) - (before_v.get("pri") or 0)
            if abs(d_cur) > 0.005 or abs(d_pri) > 0.005:
                value_diffs.append({
                    "label": label,
                    "before_cur": before_v.get("cur") or 0,
                    "after_cur":  after_v.get("cur") or 0,
                    "delta_cur":  d_cur,
                    "before_pri": before_v.get("pri") or 0,
                    "after_pri":  after_v.get("pri") or 0,
                    "delta_pri":  d_pri,
                })

    app.logger.info(f"[{source_tag}_fix] applied={len(applied)} skipped={len(skipped)} "
                     f"after_unmatched={after_count} resolved={len(resolved_labels)} "
                     f"values_changed={len(value_diffs)}")

    # ── Record ───────────────────────────────────────────────
    record = {
        "ts":                       time.strftime("%Y-%m-%d %H:%M:%S"),
        "source":                   source_tag,           # 'ai' or 'manual'
        "n_applied":                len(applied),
        "n_skipped":                len(skipped),
        "before_unmatched_count":   before_count,
        "after_unmatched_count":    after_count,
        "resolved_count":           len(resolved_labels),
        "resolved_labels":          resolved_labels,
        "value_diffs":              value_diffs,
        "applied":                  applied,
        "skipped":                  skipped,
    }
    result.setdefault("manual_fixes", []).append(record)
    # Stash a quick "last action" for the result page to flash at the top
    result["last_action"] = {
        "ts": record["ts"],
        "source": source_tag,
        "summary": (f"Applied {len(applied)} mapping(s). "
                     f"Resolved {len(resolved_labels)} row(s). "
                     f"{after_count} still unmatched."),
        "applied_count":   len(applied),
        "resolved_count":  len(resolved_labels),
        "before_count":    before_count,
        "after_count":     after_count,
        "value_diff_count": len(value_diffs),
    }
    res_path.write_text(json.dumps(result, indent=2, default=str),
                        encoding="utf-8")

    return {
        "ok":                True,
        "summary":           result["last_action"]["summary"],
        "applied":           applied,
        "skipped":           skipped,
        "before_unmatched":  before_count,
        "after_unmatched":   after_count,
        "resolved_labels":   resolved_labels,
        "value_diffs":       value_diffs,
        "values":            result.get("values"),
        "unmatched":         result.get("unmatched"),
    }


def _do_auto_fix(job: str) -> Dict[str, Any]:
    """Core auto-fix loop, callable from /api/ai/auto_fix and from
    /process (if auto_ai checkbox was ticked).

    Steps:
      1. Load the job result + AI config + pattern catalog.
      2. Take all unmatched labels and send to OpenAI in one batch.
      3. For classifications with confidence >= threshold AND a CSV-backed
         pattern, append the raw label as a new keyword variation in the
         appropriate CSV. Rebuild patterns.json.
      4. Re-run rule engine + re-write the filled template.
      5. Append a run record to result['ai_runs'] and persist.

    Returns a summary dict the API hands back to the UI.
    """
    job_dir = OUTPUT_DIR / job
    res_path = job_dir / "_result.json"
    if not res_path.exists():
        raise RuntimeError(f"Job {job!r} not found.")
    result = json.loads(res_path.read_text(encoding="utf-8"))
    if result.get("error"):
        raise RuntimeError(f"Job failed earlier: {result['error']}")

    cfg = _load_ai_config()
    key = _resolve_api_key(cfg)
    if not key:
        raise ConfigurationError(
            "No OpenAI API key. Open /settings and paste one.")

    model = cfg.get("model", "gpt-4o-mini")
    threshold = float(cfg.get("auto_apply_threshold", 0.7))

    unmatched = result.get("unmatched") or []
    if not unmatched:
        return {"ok": True, "classifications": [], "auto_added": [],
                "still_unmatched": [], "summary": "Nothing to classify."}

    labels = [u["label"] for u in unmatched if (u.get("label") or "").strip()]
    section_hints = {u["label"]: u.get("section")
                      for u in unmatched if u.get("section")}

    patterns_doc = _load_json(PATTERNS_FILE)
    patterns = patterns_doc.get("patterns", {})

    clf = AIClassifier(key, model=model)
    classifications = clf.classify_labels(labels, patterns, section_hints)

    # Apply high-confidence suggestions to the CSV dictionary
    specs = STORE.load_specs()
    auto_added: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for c in classifications:
        if not c.pattern_id or c.confidence < threshold:
            rejected.append(c.as_dict())
            continue
        spec = specs.get(c.pattern_id)
        if not spec or not spec.csv_source or not spec.csv_category:
            rejected.append({**c.as_dict(),
                             "skip_reason": "pattern is hand-curated"})
            continue
        try:
            added = STORE.add_keyword(spec.csv_source, spec.csv_category,
                                       c.label)
        except Exception as e:
            rejected.append({**c.as_dict(), "skip_reason": str(e)})
            continue
        auto_added.append({
            **c.as_dict(),
            "csv_source": spec.csv_source,
            "csv_category": spec.csv_category,
            "added_to_csv": added,
        })

    if auto_added:
        STORE.build_patterns()
        # Re-run the rule engine + re-write filled template
        rr = _apply_rules_and_write(
            extracted_xlsx=result["extracted_xlsx"],
            sheet_preference=result.get("sheet_preference", "standalone"),
            units=result.get("units", "lakhs"),
            company=result["company"],
            fy_label=result.get("fy_label", ""),
            out_xlsx=Path(result["filled_xlsx"]),
        )
        result["values"]    = rr["values_display"]
        result["values_raw"] = rr["values_raw"]
        result["unmatched"] = rr["unmatched"]
        result["filled_xlsx"] = rr["filled_xlsx"]

    # Record the run
    run_record = {
        "ts":             time.strftime("%Y-%m-%d %H:%M:%S"),
        "model":          model,
        "n_classified":   len(classifications),
        "n_auto_added":   len(auto_added),
        "threshold":      threshold,
        "auto_added":     auto_added,
        "rejected":       rejected,
    }
    result.setdefault("ai_runs", []).append(run_record)
    res_path.write_text(json.dumps(result, indent=2, default=str),
                        encoding="utf-8")

    return {
        "ok":               True,
        "summary":          (f"Classified {len(classifications)}; "
                             f"added {len(auto_added)} to CSV; "
                             f"{len(rejected)} rejected (low confidence or "
                             f"hand-curated pattern)."),
        "classifications":  [c.as_dict() for c in classifications],
        "auto_added":       auto_added,
        "rejected":         rejected,
        "values":           result.get("values"),
        "unmatched":        result.get("unmatched"),
    }


@app.post("/api/ai/auto_fix")
def api_ai_auto_fix():
    body = request.get_json(force=True, silent=True) or {}
    job = (body.get("job") or "").strip()
    if not job:
        return _api_err("job is required.")
    try:
        return jsonify(_do_auto_fix(job))
    except ConfigurationError as e:
        return _api_err(str(e), 400)
    except ClassifierError as e:
        return _api_err(str(e), 502)
    except Exception as e:
        traceback.print_exc()
        return _api_err(f"{type(e).__name__}: {e}", 500)


@app.post("/api/manual_fix")
def api_manual_fix():
    """User-driven mapping: body is {job, mappings:[{label, pattern_id}, ...]}.
    Each non-empty pair appends the label to the chosen pattern's CSV bucket,
    rebuilds patterns.json, re-runs the rule engine, and re-writes the
    filled template -- without any AI in the loop."""
    body = request.get_json(force=True, silent=True) or {}
    job = (body.get("job") or "").strip()
    mappings = body.get("mappings") or []
    if not job:
        return _api_err("job is required.")
    if not isinstance(mappings, list):
        return _api_err("mappings must be a list of {label, pattern_id} pairs.")
    if not mappings:
        return jsonify({"ok": True, "summary": "No mappings to apply.",
                        "applied": [], "skipped": []})
    try:
        return jsonify(_apply_label_mappings(job, mappings, source_tag="manual"))
    except Exception as e:
        traceback.print_exc()
        return _api_err(f"{type(e).__name__}: {e}", 500)


@app.post("/api/ai/suggest_unmatched")
def api_ai_suggest_unmatched():
    """Given the unmatched rows from a job, ask the model to suggest the best
    pattern for each (no auto-apply). The /result page calls this to
    pre-select the manual-mapping dropdown for each unmatched row.

    Body: {job}. Returns: {ok, suggestions: [{label, pattern_id, confidence,
    reasoning}, ...]}.
    """
    body = request.get_json(force=True, silent=True) or {}
    job = (body.get("job") or "").strip()
    if not job:
        return _api_err("job is required.")
    job_dir = OUTPUT_DIR / job
    res_path = job_dir / "_result.json"
    if not res_path.exists():
        return _api_err("Job not found.", 404)
    result = json.loads(res_path.read_text(encoding="utf-8"))
    unmatched = result.get("unmatched") or []
    if not unmatched:
        return jsonify({"ok": True, "suggestions": []})

    cfg = _load_ai_config()
    key = _resolve_api_key(cfg)
    if not key:
        return _api_err("No OpenAI API key configured. Open /settings.", 400)

    labels = [u["label"] for u in unmatched]
    hints = {u["label"]: u.get("section") for u in unmatched if u.get("section")}
    patterns = _load_json(PATTERNS_FILE).get("patterns", {})
    try:
        clf = AIClassifier(key, model=cfg.get("model", "gpt-4o-mini"))
        classifications = clf.classify_labels(labels, patterns, hints)
    except (ConfigurationError, ClassifierError) as e:
        return _api_err(str(e), 502)
    return jsonify({
        "ok": True,
        "suggestions": [c.as_dict() for c in classifications],
        "model": cfg.get("model", "gpt-4o-mini"),
    })


@app.post("/api/ai/chat")
def api_ai_chat():
    """Conversational editor. The browser keeps the message history and
    sends the full list on every turn. Body:
        {messages: [{role: 'user'|'assistant', content: '...'}, ...]}
    Returns:
        {ok, reply, actions, transcript}
    """
    body = request.get_json(force=True, silent=True) or {}
    messages = body.get("messages") or []
    if not isinstance(messages, list):
        return _api_err("messages must be a list.")
    if not messages:
        return _api_err("messages cannot be empty.")
    # The browser may send {role, content}; nothing else is trusted.
    clean: List[Dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict): continue
        role = m.get("role")
        content = m.get("content") or ""
        if role in ("user", "assistant") and content:
            clean.append({"role": role, "content": content[:8000]})
    if not clean:
        return _api_err("No usable messages in payload.")

    cfg = _load_ai_config()
    key = _resolve_api_key(cfg)
    if not key:
        return _api_err("No OpenAI API key configured. Open /settings.", 400)

    ctx = ToolContext(
        store=STORE,
        rules_file=RULES_FILE,
        patterns_file=PATTERNS_FILE,
        rules_default_file=DATA_DIR / "rules.default.json",
    )
    try:
        asst = Assistant(api_key=key, model=cfg.get("model", "gpt-4o-mini"))
        result = asst.chat(clean, ctx)
    except (AsstConfigError, AssistantError) as e:
        return _api_err(str(e), 502)
    except Exception as e:
        traceback.print_exc()
        return _api_err(f"{type(e).__name__}: {e}", 500)
    return jsonify({"ok": True, **result})


# ─── Startup ────────────────────────────────────────────────────────

def _bootstrap():
    """First-run init:
       1. Snapshot rules.json -> rules.default.json so 'Reset' works.
       2. Build patterns.json from CSVs + specs (idempotent, fast).
          Removes any pre-existing patterns.json first so an old file
          written with a wrong encoding (e.g. cp1252 leftover from a
          previous Windows boot) can never be served if rebuild fails."""
    snap = DATA_DIR / "rules.default.json"
    if not snap.exists() and RULES_FILE.exists():
        shutil.copy(RULES_FILE, snap)
    try:
        if PATTERNS_FILE.exists():
            try: PATTERNS_FILE.unlink()
            except Exception: pass
        STORE.build_patterns()
    except Exception as e:
        app.logger.error(f"Pattern build failed at startup: {e}")


_bootstrap()


if __name__ == "__main__":
    print("─" * 64)
    print(" AskMyCFO Simple Template Filler")
    print(f"   Upload:    http://127.0.0.1:5005/")
    print(f"   Rules:     http://127.0.0.1:5005/rules")
    print(f"   Keywords:  http://127.0.0.1:5005/keywords")
    print(f"   Settings:  http://127.0.0.1:5005/settings   (OpenAI api key + model)")
    print("─" * 64)
    port = int(os.environ.get("PORT", 5005))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
