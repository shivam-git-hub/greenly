# DCF Model Skill

> Builds a professional-grade DCF model in Google Sheets — historical inputs, revenue projections, WACC, terminal value, sensitivity analysis, and equity bridge.

A step-by-step guide for building a professional-grade Discounted Cash Flow (DCF) model in Google Sheets. Follow every section in order. Never skip clarifications. Never hard-code assumptions inside formula cells.

---

## 1. Clarifications — Ask Before Writing a Single Cell

Do not begin building until you have answers to every item below. If the user has not provided them, ask in a single consolidated message.

### 1.1 Company & Scope
- Company name, ticker, industry
- Reporting currency and unit scale (e.g. USD millions)
- Fiscal year end (calendar or non-calendar)
- Is this a public or private company?

### 1.2 Historical Data
- How many years of historical financials are available? (Minimum 3 recommended, ideally 5)
- Are the financials already in the sheet, or does the user need to enter them?
- Are there non-recurring / one-off items to strip out? (restructuring, impairments, gains on asset sales)

### 1.3 Projection Scope
- Projection period: 5, 7, or 10 years? (Default: 5 years explicit + terminal value)
- FCF type: **FCFF** (Firm, for EV) or **FCFE** (Equity, for equity value directly)? Default: FCFF → Enterprise Value bridge.
- Revenue model: single growth rate, segment-by-segment, or driver-based (volume × price)?

### 1.4 Key Assumptions (confirm or accept defaults)
- Revenue growth rates per year, or a single CAGR to phase down
- EBIT margin trajectory (start, end, years to reach steady state)
- Tax rate: statutory or effective? Normalize to which?
- D&A as % of revenue or absolute figure
- CapEx as % of revenue or absolute figure
- NWC (Net Working Capital) as % of revenue, or individual line drivers
- Terminal growth rate (g): typically 2–3% for developed markets; must be < WACC
- Terminal value method: **Gordon Growth Model** (default) or **Exit EV/EBITDA multiple**

### 1.5 Capital Structure & WACC
- Cost of equity method: CAPM (default) or user-supplied?
  - If CAPM: risk-free rate, equity risk premium, beta (levered)
- Cost of debt: pre-tax rate (look up or user-supplied)
- Marginal tax rate for tax shield
- Capital structure weights: use current market weights or target weights?
- Are there preferred shares? (adds third WACC component)

### 1.6 Equity Bridge Inputs
- Current net debt (total debt − cash & equivalents) or net cash position
- Minority interest (book value)
- Associates / equity investments at market value
- Total diluted shares outstanding (for per-share value)
- Any other non-operating assets or liabilities

### 1.7 Output Preferences
- Sensitivity tables: which two variables? (Default: WACC × terminal growth rate)
- Should the model include a football field / waterfall summary chart?

---

## 2. Sheet Architecture

Build the model across **named tabs in this exact order**. Never put everything on one sheet.

| Tab Name        | Purpose |
|-----------------|---------|
| `Assumptions`   | Every hard-coded input lives here. Colour inputs blue. |
| `Historicals`   | Raw historical IS, BS, CF data. No formulas that change projections. |
| `Income_Stmt`   | Projected income statement (links to Assumptions). |
| `Working_Cap`   | NWC schedule — AR, Inventory, AP, Other WC. |
| `CapEx_DA`      | Capital expenditure and D&A schedule. |
| `FCF`           | Free cash flow build from EBIT down. |
| `WACC`          | WACC calculation with all components shown. |
| `DCF_Output`    | PV of FCFs, terminal value, EV, equity bridge, implied share price. |
| `Sensitivity`   | Two-variable data tables (WACC × g, WACC × exit multiple, etc.). |
| `Dashboard`     | (Optional) summary KPIs and charts for presentation. |

### Design Rules — Non-Negotiable
1. **All hard-coded numbers live only in `Assumptions`**. Every other sheet references `Assumptions` cells. Never type a number directly into a formula on any other sheet.
2. **Blue cells = inputs** (hard-coded). **White cells = formulas**. Apply cell formatting accordingly so the model is self-documenting.
3. **Time runs left to right**: Column A = labels, Column B = most recent historical year, columns after = projection years. Use a year header row on every sheet.
4. **Label every row** with a short description in Column A. Include units where ambiguous.
5. **No circular references** — handle interest expense circularity by using prior-year debt balance (acceptable approximation) or a dedicated revolver/debt schedule with an iteration flag clearly documented.
6. **Percentage inputs stored as decimals** (0.10 for 10%, not 10). Format the display with % number format; store the value as decimal for formulas.

---

## 3. Assumptions Sheet — What to Include

Organise into clearly labelled sections with bold headers. Every cell that feeds the model must be here.

```
SECTION: General
  - Company Name
  - Projection Start Year
  - Projection Years (n)
  - Currency
  - Units (millions / thousands)

SECTION: Revenue
  - Growth rate Year 1..n (individual cells, or base + step-down)
  - If segment model: revenue per segment + growth per segment

SECTION: Margins & Costs
  - COGS % of Revenue (or Gross Margin %)
  - SG&A % of Revenue
  - R&D % of Revenue (if applicable)
  - EBIT Margin (derived — show as a check, not an input)
  - Normalised tax rate

SECTION: D&A & CapEx
  - D&A % of Revenue (or prior-year PP&E %)
  - CapEx % of Revenue
  - CapEx / D&A ratio as a sanity check row (flag if < 1.0 for a growth company)

SECTION: Working Capital (% of Revenue drivers)
  - Days Sales Outstanding (DSO) → AR
  - Days Inventory Outstanding (DIO) → Inventory
  - Days Payable Outstanding (DPO) → AP
  - Other current assets % Revenue
  - Other current liabilities % Revenue

SECTION: WACC
  - Risk-free rate
  - Equity risk premium
  - Beta (levered)
  - Cost of equity (CAPM formula, shown here)
  - Pre-tax cost of debt
  - Tax rate for shield
  - Debt weight (% of capital)
  - Equity weight (% of capital)
  - WACC (formula, shown here as a cross-check)

SECTION: Terminal Value
  - Method (label only — GGM or Exit Multiple)
  - Terminal growth rate (g) — GGM
  - Exit EV/EBITDA multiple — if exit multiple method
  - Mid-year convention toggle (TRUE/FALSE)

SECTION: Equity Bridge
  - Net Debt (positive = net debt, negative = net cash)
  - Minority Interest
  - Associates (market value)
  - Diluted shares outstanding
```

---

## 4. Income Statement Projection

Build on `Income_Stmt` tab. Columns: Label | Hist Y-2 | Hist Y-1 | Hist Y0 | Proj Y1 … Proj Yn

**Row order (standard):**
```
Revenue
  % growth (row, formula)
COGS
Gross Profit
  Gross Margin % (row, formula)
SG&A
R&D (if applicable)
EBITDA
  EBITDA Margin % (row, formula)
D&A
EBIT (Operating Income)
  EBIT Margin % (row, formula)
Interest Expense  [use prior-year debt × cost of debt — avoids circularity]
Interest Income   [cash × short-term rate]
EBT
Tax
Net Income
  Net Margin % (row, formula)
```

**Notes:**
- Separate EBITDA as an explicit line — it feeds the exit multiple terminal value and FCFF calculation
- Never net interest income against interest expense — show both gross
- Tax = EBT × normalised tax rate from Assumptions (not effective historical rate, unless user confirmed)

---

## 5. Working Capital Schedule

Build on `Working_Cap` tab.

```
Revenue (link from Income_Stmt)
Accounts Receivable    = Revenue × DSO / 365
Inventory              = COGS × DIO / 365
Accounts Payable       = COGS × DPO / 365
Other Current Assets   = Revenue × assumption %
Other Current Liabilities = Revenue × assumption %

Net Working Capital (NWC) = AR + Inventory + OCA − AP − OCL
Change in NWC (ΔNWC)     = NWC(t) − NWC(t−1)
  [Positive ΔNWC = cash outflow in FCF — label clearly]
```

**Validation:** Show historical NWC as % of revenue. Flag if any projection year deviates > 5pp from the last historical year without a stated reason.

---

## 6. CapEx & D&A Schedule

Build on `CapEx_DA` tab.

```
Revenue (link)
CapEx                  = Revenue × CapEx % assumption
D&A                    = Revenue × D&A % assumption   [or prior PP&E × rate]
Net CapEx (CapEx − D&A)
PP&E (Opening)         = Prior year closing
PP&E Additions         = CapEx
Less: D&A              = (D&A)
PP&E (Closing)         = Opening + Additions − D&A

Sanity check row: CapEx / D&A ratio  [flag < 1 for growth companies]
```

---

## 7. Free Cash Flow Build

Build on `FCF` tab. This is the heart of the model.

**FCFF (standard):**
```
EBIT                         (link from Income_Stmt)
Less: Tax on EBIT            = EBIT × tax rate   [NOT tax on EBT — FCFF is pre-financing]
NOPAT (Net Operating Profit After Tax)
Add: D&A                     (link from CapEx_DA)
Less: CapEx                  (link from CapEx_DA)
Less: Change in NWC (ΔNWC)  (link from Working_Cap — positive ΔNWC is a subtraction)
= Unlevered Free Cash Flow (UFCF / FCFF)
```

**Discount factors:**
```
Discount period              = 1, 2, 3 … n   [or 0.5, 1.5 … if mid-year convention]
Discount factor              = 1 / (1 + WACC) ^ period
PV of FCFF                   = FCFF × Discount factor
Sum of PV(FCFF)
```

**Mid-year convention:** If `Assumptions!mid_year = TRUE`, use period = 0.5, 1.5, 2.5 … This assumes cash flows arrive evenly throughout the year rather than at year-end. Confirm with user — mid-year is more accurate for operating businesses.

---

## 8. Terminal Value

On `FCF` tab, below the explicit period, add a clearly labelled terminal value section.

**Gordon Growth Model (perpetuity growth):**
```
Terminal Year FCFF           (last projection year FCF)
Terminal Value (TV)          = Terminal FCFF × (1 + g) / (WACC − g)
PV of Terminal Value         = TV / (1 + WACC) ^ n   [or × terminal discount factor]
```

**Exit Multiple:**
```
Terminal Year EBITDA         (link from Income_Stmt)
Exit EV/EBITDA Multiple      (from Assumptions)
Terminal Value (TV)          = Terminal EBITDA × Multiple
PV of Terminal Value         = TV / (1 + WACC) ^ n
```

**TV as % of Total EV (mandatory check row):**
```
TV % of EV = PV(TV) / (Sum PV FCF + PV TV)
```
Flag (with conditional formatting, red cell) if TV% > 80%. This is a signal that the explicit period is doing too little work — either extend the projection period or revisit assumptions.

---

## 9. WACC Calculation

Build on `WACC` tab. Show every intermediate step — never bury WACC in a single cell.

```
CAPM Cost of Equity:
  Risk-free rate              (link Assumptions)
  Equity risk premium         (link Assumptions)
  Beta (levered)              (link Assumptions)
  Cost of Equity (Ke)         = Rf + β × ERP

After-tax Cost of Debt:
  Pre-tax cost of debt (Kd)   (link Assumptions)
  Tax rate                    (link Assumptions)
  After-tax Kd                = Kd × (1 − tax rate)

Capital structure weights:
  Equity weight (We)          (link Assumptions)
  Debt weight (Wd)            (link Assumptions)
  Check: We + Wd              = must equal 1.0 (show validation)

WACC = We × Ke + Wd × Kd_after_tax

Sanity checks (show as labelled rows):
  WACC > after-tax Kd?        TRUE/FALSE
  WACC < Ke?                  TRUE/FALSE
  WACC − g > 0?               TRUE/FALSE  [g from Assumptions]
  If any check is FALSE → red cell, model cannot proceed
```

---

## 10. DCF Output & Equity Bridge

Build on `DCF_Output` tab. This is the single sheet the user presents.

```
── Enterprise Value Build ──────────────────────────────
Sum of PV(FCFF)              (link from FCF)
PV of Terminal Value         (link from FCF)
Enterprise Value (EV)        = Sum PV FCFF + PV TV

── Equity Bridge ───────────────────────────────────────
Less: Net Debt               (link Assumptions — positive = debt, negative = cash)
Less: Minority Interest      (link Assumptions)
Add:  Associates             (link Assumptions)
= Equity Value

÷ Diluted Shares Outstanding (link Assumptions)
= Implied Share Price

── Context ─────────────────────────────────────────────
Current Share Price          (user input in Assumptions)
Premium / (Discount)         = Implied / Current − 1
EV / EBITDA (implied)        = EV / Terminal Year EBITDA
EV / Revenue (implied)       = EV / Terminal Year Revenue
```

---

## 11. Sensitivity Analysis

Build on `Sensitivity` tab.

Always produce **at minimum two tables**:

**Table 1 — WACC × Terminal Growth Rate (g)**
- Row headers: WACC ± 100bps in 25bps steps (5 rows)
- Column headers: g ± 75bps in 25bps steps (5 columns)
- Cell value: Implied Share Price

**Table 2 — WACC × Exit Multiple** (only if exit multiple method used, or show as alternative)
- Row headers: WACC range (same as above)
- Column headers: Exit EV/EBITDA ± 1.5× in 0.5× steps
- Cell value: Implied Share Price

Apply conditional formatting to both tables (green = above current price, red = below).

**Implementation note:** Google Sheets does not have native data tables like Excel. Implement using a helper grid that substitutes override values into named cells, or use `IF`-based formula arrays. Do NOT use macros. Build each cell as a standalone formula referencing the override row/column headers.

---

## 12. Validation Checks — Run After Every Build

Add a `Checks` section (can be a hidden area at the bottom of `DCF_Output` or a separate tab). Every check must show GREEN (pass) or RED (fail) using conditional formatting.

### Formula-Level Checks
| Check | Formula Logic |
|-------|--------------|
| Revenue growth never exceeds 100% in any year | MAX(growth rates) < 1 |
| EBIT margin stays positive in all projection years | MIN(EBIT margins) > 0 |
| WACC > terminal growth rate | WACC − g > 0 |
| WACC is between Kd_after_tax and Ke | Kd_after < WACC < Ke |
| CapEx / D&A ≥ 0.5 in all years | MIN(CapEx/DA) ≥ 0.5 |
| Discount factors are strictly decreasing | Each factor < prior factor |
| PV(TV) as % of EV < 80% | PV_TV / EV < 0.8 |
| NWC % of revenue stays within ±10pp of last historical | |
| Shares outstanding > 0 | |
| All FCF discount periods are positive | |

### Structural Checks (confirm manually before delivering)
- No hard-coded numbers in formula cells outside `Assumptions` tab
- Every projection year column has the same formula structure (use array formulas or copy-paste verification)
- Historical columns are not accidentally overwritten by projection formulas
- `=IFERROR` wrappers are not hiding real errors — only use where divide-by-zero is structurally expected (e.g. growth rate on year 1 if no prior year)

---

## 13. Common Errors — Avoid These

| Error | Why It Happens | How to Prevent |
|-------|---------------|----------------|
| Using EBT tax instead of EBIT tax in FCFF | Copy-paste from a levered model | FCFF tax = EBIT × rate, explicitly labelled |
| Circular reference on interest expense | Current debt × current rate | Use prior-year debt balance as proxy |
| Terminal value dominates (>85% of EV) | Projection period too short or g too high | Extend period; cross-check g vs GDP growth |
| Mixing nominal and real cash flows | Using real GDP growth for g with nominal WACC | Both must be nominal, or both real |
| Book value weights in WACC | Using balance sheet debt/equity | Use market cap for equity weight |
| Forgetting to normalise terminal year FCFF | Last projection year has lumpy CapEx | Confirm terminal year CapEx ≈ D&A (steady state) |
| Double-counting cash | Net debt uses gross debt but cash is also added back elsewhere | Net debt = gross debt − cash, used once only |
| Minority interest direction | Adding instead of subtracting | MI is a claim on EV → subtract in equity bridge |
| Mid-year inconsistency | TV discounted at n, FCFs at mid-year | TV period must match FCF convention |
| Non-recurring items in terminal FCFF | One-off restructuring charge in terminal year | Explicitly normalise terminal year margins |

---

## 14. Build Sequence (Step-by-Step Order)

Follow this exact order — later steps depend on earlier ones.

1. Create all tabs with correct names and year headers
2. Build `Assumptions` tab completely — fill all inputs before touching other tabs
3. Build `Historicals` — enter/link raw data, strip non-recurring items
4. Build `Income_Stmt` — project revenue → EBITDA → EBIT
5. Build `Working_Cap` — NWC schedule and ΔNWC
6. Build `CapEx_DA` — CapEx and D&A schedule, PP&E roll-forward
7. Build `WACC` tab — all components and validation checks
8. Build `FCF` — NOPAT → UFCF → discounting → PV sum
9. Add terminal value section to `FCF`
10. Build `DCF_Output` — equity bridge and implied price
11. Build `Sensitivity` tables
12. Run all validation checks → resolve any RED cells
13. Format: blue inputs, clean number formats, freeze header row on each tab
14. Deliver a brief summary to the user: key assumptions, implied price, TV%, WACC

---

## 15. Formatting Standards

- **Blue fill (#CFE2F3), black text** — hard-coded input cells
- **White fill** — formula cells
- **Light grey fill (#F3F3F3)** — section header rows (bold text)
- **Bold + bottom border** — subtotal rows (Gross Profit, EBITDA, EBIT, NOPAT, FCFF, EV, Equity Value)
- **Red fill (#F4CCCC)** — failed validation checks
- **Green fill (#D9EAD3)** — passed validation checks
- Number formats:
  - Currency values: `#,##0.0` (one decimal, no symbol unless header says otherwise)
  - Percentages: `0.0%`
  - Multiples: `0.0x`
  - Per-share price: `0.00`
  - Growth rates: `+0.0%;-0.0%;—`

---

## 16. What to Tell the User at Delivery

After building, always output a plain-language summary covering:
1. **Implied share price range** from the sensitivity table (low / base / high)
2. **Key value drivers**: which 2–3 assumptions most affect the result
3. **TV as % of EV**: flag if high and explain what it means
4. **WACC used** and its components
5. **Any assumptions the user should revisit** (e.g. if terminal growth rate equals or exceeds WACC, model is broken and will produce a negative or infinite value)
6. **Limitations**: what the model does not capture (synergies, optionality, balance sheet stress)
