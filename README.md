# AskMyCFO — Simple Template Filler

A focused Flask app that takes one annual-report PDF and gives you back a
filled `Input_Templates.xlsx` ready for downstream KPI/diagnostic work.

```
PDF → detect BS/PL pages → trimmed PDF → extracted xlsx (as-is)
                                            ↓
                              rule engine (CSV dictionary + rules)
                                            ↓
                                  filled Input_Templates.xlsx
```

Three editable surfaces, three pages:

| Page         | What you edit                                     | Where it lives                          |
|--------------|---------------------------------------------------|-----------------------------------------|
| `/`          | Upload a PDF, run pipeline                        | —                                       |
| `/rules`     | Formulas (target field = combo of patterns)       | `data/rules.json`                       |
| `/keywords`  | The keyword dictionary (per-pattern variations)   | `data/keywords/{bs,pl}_keywords.csv`    |


## Quick start

```bash
chmod +x run.sh
./run.sh                       # installs deps, starts http://127.0.0.1:5005
```

Then:

1. Open `/keywords` — your dictionary, as you uploaded it. Add variations
   when a report uses unusual phrasing. Edits write back to your CSVs.
2. Open `/rules` — confirm the 35 formulas line up with your template doc.
   Each rule shows a plain-English preview at the top of the card.
3. Open `/` — upload an annual-report PDF, hit Run. Download the filled
   template.


## Architecture: the dictionary is the source of truth

Your two CSVs are preserved verbatim:

```
data/keywords/
├── bs_keywords.csv          ← your 30 BS categories × 200 variations
└── pl_keywords.csv          ← your 11 PL categories × 500 variations
```

Around them sit two configuration files and one cached runtime file:

```
data/
├── pattern_specs.json       ← declarative catalog (43 patterns):
│                              pattern_id → {CSV bucket, group, section_filter, extras}
├── rules.json               ← the 35 formulas you can edit on /rules
├── rules.default.json       ← snapshot for the "Reset" button
├── cell_map.json            ← template label → row mapping
└── patterns.json            ← AUTO-GENERATED; do not edit
```

**When you add a keyword via `/keywords`:**

1. New row appended to the relevant CSV (`bs` or `pl`)
2. `patterns.json` rebuilt from CSVs + `pattern_specs.json`
3. Rule engine picks up the new alias on next run

**When you delete a keyword:** same flow in reverse — row removed from CSV,
`patterns.json` rebuilt, `.bak` backup of the CSV kept alongside.

### Why 43 patterns when your CSV has 41 buckets?

Two extra hand-curated patterns cover line items that don't have a CSV
bucket of their own but are common in Schedule III reports:

- `deferred_tax_liabilities` — DTL appears on its own row in most reports
  and is referenced by two template rules per your doc
- `lease_liabilities_noncurrent` — Ind AS 116 split that the CSV bundles
  with other NCL
- `current_tax_liabilities`, `exceptional_items`, `impairment_loss` —
  optional patterns you can wire into rules if needed

These hand-curated patterns are shown read-only on `/keywords` (they live
in `pattern_specs.json`, not in the editable CSVs).


## Schedule III completeness — what's covered

Every line item in your template doc maps to a pattern that pulls from
~100-200 keyword variations from your CSV. The `extras` in
`pattern_specs.json` add common Indian-accounting phrasings the CSV may
miss, including:

- **Equity:** securities premium, capital reserve, capital redemption
  reserve, debenture redemption reserve, hedging reserve, FCTR, share
  warrants, share application money pending allotment, equity component
  of compound instruments
- **Current Liab:** MSME vs Others split, advance from customers,
  contract liabilities (Ind AS 115), statutory dues, GST/TDS payable,
  current maturities of LT debt, unclaimed dividends, security
  deposits payable, capital creditors, interest accrued
- **Non-Current Liab:** debentures, bonds, NCDs, government grants,
  long-term security deposits
- **PPE/Fixed Assets:** PPE + RoU + Goodwill + Intangibles + Other
  Intangibles all bucketed → one rule sums them automatically
- **Investments:** Investment Property + Financial Investments + NC
  Investments + Investments in Subsidiaries / Associates / JVs
- **Other NC Assets:** capital advances, biological assets, MAT credit
  entitlement, prepaid lease rent
- **Current Assets:** stock-in-trade, finished goods, raw materials,
  WIP, stores and spares, current tax assets, advance tax, TDS
  receivable, GST receivable, export incentives, contract assets
- **P&L:** sale of goods + services + other operating revenue (all under
  Revenue); cost of materials + purchases + inventory changes (under
  COGS); current + MAT + deferred + prior-period (under Taxes)

When MSME and Others sub-rows BOTH match the single `trade_payables`
pattern, the rule engine sums them automatically. So `Accounts Payable`
= 1 operand. 28 of 35 rules are 1-operand.


## How patterns work at runtime

Each pattern resolves to an alias list at build time. The matcher is
substring + longest-alias-wins:

```jsonc
"trade_payables": {
  "name": "Trade Payables (MSME + Others + Outstanding Dues)",
  "group": "current_liab",
  "section_filter": null,
  "csv_source": "bs",
  "csv_category": "Trade Payables",
  "aliases": [
    "total outstanding dues of micro and small enterprises",
    "outstanding dues of creditors other than micro",
    "trade payables msme", "trade payables others",
    "trade creditors per note", "sundry creditors",
    "accounts payable gross", "accounts payable amount",
    /* ~200 more drawn from your CSV */
  ]
}
```

A non-destructive noise filter runs at build time only — it skips
fragments like `"for the year"`, `"consolidated"`, `"ind as"` (which
came from CSV tail tokens after prefix-stripping) so they don't cause
spurious matches. Your CSV stays untouched.


## Rules screen — what you see

Each rule is one card. The header shows a plain-English formula preview
that updates live:

```
Accounts Payable = Trade Payables
Other Current Liabilities = Other Current Liabilities + Deferred Tax Liabilities (Net)
Other expenses less other income = Other Income − Other Expenses
Fixed Assets = Fixed Assets (PPE + RoU + Goodwill + Intangibles)
```

Below the preview, each operand is one row: operator dropdown (`+`, `−`,
`×`, `÷`) + pattern dropdown (grouped by section) + remove button.

The save button validates every operand: every `pattern` must exist in
the runtime catalog, every `op` must be in `{+, -, *, /}`. Server-side
backup is written to `data/rules.backup.json` on every save. Click
**Reset to defaults** to restore `rules.default.json`.


## Keywords screen — managing the dictionary

One card per pattern. Each card shows:

- The pattern's CSV source (e.g. `BS CSV ▸ Trade Payables`) and counts
- An add-row at the top: type a new variation → Enter or click Add →
  it's appended to the CSV
- A collapsible **CSV keywords** list: every variation currently in your
  CSV bucket, with `✕` buttons to remove
- A collapsible **Hand-curated extras** list, read-only (these live in
  `pattern_specs.json`)

A filter input at the top narrows by pattern name, CSV bucket, or
keyword content.


## API

| Method | Path                          | Purpose                              |
|--------|-------------------------------|--------------------------------------|
| GET    | `/api/patterns`               | Runtime pattern catalog              |
| GET    | `/api/cell_map`               | Template label → row map             |
| GET    | `/api/rules`                  | Current rules                        |
| POST   | `/api/rules`                  | Save rules (validated)               |
| POST   | `/api/rules/reset`            | Restore defaults                     |
| GET    | `/api/keywords`               | List patterns + CSV keywords + extras|
| POST   | `/api/keywords/add`           | Append keyword to CSV bucket         |
| POST   | `/api/keywords/delete`        | Remove keyword from CSV bucket       |


## File layout

```
askmycfo_template_filler/
├── app.py                          # Flask app, ~280 lines
├── run.sh
├── requirements.txt
├── README.md                       # you are here
├── Input_Templates.xlsx            # your master template (preserved)
├── modules/
│   ├── page_detector.py            # your file, verbatim
│   ├── extract_tables.py           # your file, verbatim
│   ├── rule_engine.py              # ~280 lines — substring matcher + bucketer + writer
│   ├── template_writer.py          # ~130 lines — fills the .xlsx
│   └── keyword_store.py            # ~280 lines — CSV CRUD + pattern derivation
├── data/
│   ├── keywords/
│   │   ├── bs_keywords.csv         # your dictionary, preserved verbatim
│   │   └── pl_keywords.csv
│   ├── pattern_specs.json          # 43 patterns declarative catalog
│   ├── patterns.json               # auto-generated runtime cache
│   ├── rules.json                  # the 35 editable formulas
│   ├── rules.default.json          # snapshot for Reset
│   └── cell_map.json
├── templates/
│   ├── index.html                  # upload page
│   ├── rules.html                  # formula editor
│   ├── keywords.html               # dictionary editor
│   └── result.html                 # download page
├── static/
│   ├── style.css                   # editorial cream/terracotta theme
│   ├── rules.js                    # rule editor + formula preview
│   └── keywords.js                 # dictionary editor
├── uploads/                        # PDFs land here
└── outputs/                        # per-job result folders
```


## Verified against your template doc

- 35/35 rules match your "Input Automation Test Requirement" document
  line-by-line, including the two NCL rules
  (`Others (Non-Current Liabilities)` uses `deferred_tax_liabilities`;
  `Other Financial Liabilities (Non-Current)` includes
  `lease_liabilities_noncurrent`)
- 26/26 numeric smoke validations pass on a synthetic Wendt-style extract
- 43 patterns / ~6,000 aliases derived from your CSV
- All Flask endpoints return 200 / valid JSON
- CSV add/delete round-trip verified, hand-curated patterns correctly
  rejected with 409
