# Greenly

An AI-powered Google Sheets assistant. A sidebar add-on that lets you chat with your spreadsheet — the frontend runs entirely in Google Apps Script (GAS) while a Flask backend drives a multi-agent pipeline that reads, writes, and verifies changes on your behalf.

---

## Architecture

```
Google Sheets sidebar (GAS)
        │  HTTP POST /api/v1/chat
        ▼
Flask backend  ─────────────────────────────────────────────────────
│                                                                   │
│  chat.py  ──  planning → executing → verifying → (replanning ↻)  │
│                                                                   │
│  Agents (LangChain AgentExecutor)                                 │
│    planner      read + research tools  →  produces JSON plan      │
│    execution    write + read + python  →  carries out the plan    │
│    verifier     read + python          →  checks sheet state      │
│    replanner    read + research        →  corrects failed plans   │
│    researcher   web + filings          →  sub-agent for research  │
│    basic        (no tools)             →  writes user reply       │
│                                                                   │
│  LLM providers  (via .env)                                        │
│    Anthropic  →  Claude Haiku 4.5 (fast) / Claude Sonnet 4.6     │
│    Google     →  Gemini 2.5 Flash (fast) / Gemini 2.5 Pro        │
─────────────────────────────────────────────────────────────────────
```

---

## Project Structure

```
greenly/
├── addon/                  Google Apps Script frontend (sidebar UI)
│   └── src/
├── backend/
│   ├── main.py             Flask entry point
│   ├── llm/
│   │   ├── agentFactory.py Agent + executor construction, prompt caching, scratchpad compaction
│   │   ├── llmFactory.py   LLM client factory (Anthropic / Google)
│   │   └── prompts/        System prompts per agent (planner, execution, verifier, …)
│   ├── routes/
│   │   └── blueprints/
│   │       └── chat.py     /api/v1/chat — pipeline orchestration loop
│   ├── tools/
│   │   ├── common_tools/   Web search, URL fetch, SEC/BSE filings, Python sandbox
│   │   ├── cell_content/   write_values
│   │   ├── read_structure/ read_range, read_sheet_structure, get_chunk
│   │   ├── formatting/     apply_cell_format, conditional_formats, …
│   │   ├── charts/         create/get/delete chart
│   │   ├── pivot_tables/   create/get/delete pivot
│   │   ├── named_ranges/   create/get/delete named range
│   │   ├── data_validation/
│   │   ├── formula_utils/  audit_formulas, trace_dependents
│   │   ├── sheet_structure/ create/rename/delete sheet
│   │   └── langchain_tools.py  Wraps all tools for LangChain
│   ├── skills/
│   │   └── DCF/            SKILL.md — step-by-step DCF model instructions
│   └── utilities/
│       └── task_context.py TaskContext dataclass (shared state across agents)
├── googleOauth.py          OAuth2 token helper
└── .env                    API keys + tuning parameters (see below)
```

---

## Setup

### Prerequisites

- Python 3.11+ (conda env named `greenly`)
- A Google Cloud project with Sheets API enabled and OAuth credentials in `credentials.json`
- At least one of: `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY`

### Backend

```bash
conda activate greenly
pip install -r backend/requirements.txt

# First run — authorise Google Sheets access
python googleOauth.py

# Start the server
cd backend
python main.py          # listens on PORT (default 8080)
```

### Google Apps Script (frontend)

Deploy the `addon/` directory as a GAS project bound to your Google Sheet. The sidebar calls `https://<your-server>/api/v1/chat`.

---

## Configuration (`.env`)

```env
# Provider — "anthropic" or "google"
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
GOOGLE_API_KEY=...

# Web search (optional — DuckDuckGo used first, no key needed)
BRAVE_API_KEY=...
SERPER_API_KEY=...

# Server
PORT=8080
FLASK_DEBUG=false

# Agent behaviour (defaults shown)
LLM_MAX_TOKENS=8192
PIPELINE_MAX_ITERATIONS=50
PIPELINE_MAX_EXCEPTION_RETRIES=5
LLM_RETRY_MAX=6
LLM_RETRY_BASE_S=5
RESEARCHER_SEARCH_LIMIT=20
```

---

## Pipeline

Each request runs a state machine in `chat.py`:

```
planning ──► executing ──► verifying ──► completed
    ▲                           │
    └────── replanning ◄────────┘  (on verification failure)
```

- **Iteration cap**: `PIPELINE_MAX_ITERATIONS` (default 50) full plan→execute→verify cycles.
- **Exception handling**: execution exceptions redirect to `replanning` so the replanner can correct the approach. Other phase exceptions retry up to `PIPELINE_MAX_EXCEPTION_RETRIES` times.
- **Chat history**: persisted per spreadsheet, compacted via LLM summary when it exceeds 12 000 chars.

---

## Prompt Caching (Anthropic only)

Two cache breakpoints per LLM call:

1. **`[tools + static system prompt]`** — survives across all invocations of the same agent type.
2. **`[… + task context + history + user input]`** — within a single `agent.invoke()`, only the scratchpad changes between tool calls.

Scratchpad compaction kicks in when accumulated output exceeds 20 000 chars, keeping the last 4 steps at full detail and truncating older read/write outputs.

---

## Skills

Skills are step-by-step instruction sets for complex tasks (e.g. DCF modelling). Each skill lives in `backend/skills/<name>/SKILL.md`.

The skill catalogue (name + one-line description from each `SKILL.md`) is injected into the system prompt of planner, replanner, and verifier agents at startup. Agents call `load_skill(name)` to retrieve the full content when they need it.

---

## Research Tools

| Tool | Description |
|---|---|
| `web_search` | DuckDuckGo → Serper → Brave fallback chain |
| `web_search_financial` | Region-aware (US/IN), biased toward SEC, BSE/NSE, IR pages |
| `fetch_url` | HTML + PDF extraction (BeautifulSoup + pdfplumber), returns structured tables |
| `fetch_filing_us` | SEC EDGAR — resolves ticker → CIK, pulls 10-K/10-Q/8-K |
| `fetch_filing_india` | BSE annual reports — resolves ticker → scripcode, fetches PDF |

---

## Smoke Test

```bash
conda run -n greenly python -c "from llm.agentFactory import getAgent; getAgent('planner'); print('OK')"
```
