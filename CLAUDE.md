## Project Overview

Greenly is a Google Apps Script (GAS) add-on for Google Sheets that provides a conversational AI chatbot sidebar. It acts as a frontend bridge between Google Sheets and a backend AI service. It is highly Intelligent Excel agent like shortcut AI. 

## Backend Agent Pipeline

The Flask backend (`backend/`) runs a multi-agent state machine in `routes/blueprints/chat.py`:

`planning → executing → verifying → (replanning ↻) → completed`

Each state invokes a dedicated LangChain agent built in `llm/agentFactory.py`:

| Agent | Tools | Prompt | Role |
|---|---|---|---|
| `planner` | research + read | `planner.txt` | Produces JSON plan |
| `replanner` | research + read | `replanner.txt` | Corrective plan after verification failure |
| `execution` | write + read + python + research | `execution.txt` | Carries out the plan |
| `verifier` | read + python | `verifier.txt` | Independently verifies sheet state |
| `researcher` | research only | `researcher.txt` | (Standalone) returns structured findings |
| `generic` | all | `generic.txt` | Single-agent fallback |
| `basic` | none | (dynamic) | User-facing summarizer at end of pipeline |

`current_iteration` is incremented only on entering `planner` / `replanner`, so `max_iteration=5` means up to 5 full plan-execute-verify cycles. On exhaustion the loop sets `task_context.status = "failed"` and the basic agent writes a graceful error message.

`task_context` (a `TaskContext` dataclass from `utilities/task_context.py`) is JSON-serialized and embedded in each agent's system prompt under `## Current Task Context`.

### Anthropic prompt caching (agentFactory.py)

When `LLM_PROVIDER=anthropic`, `_build_template` renders `system` as two content blocks — `[static_prompt, task_context]` — and a `RunnableLambda` (`_apply_anthropic_cache`) inserted between `prompt` and the LLM marks block 0 with `cache_control: ephemeral` plus a second marker on the last user message. This gives two breakpoints:

1. `[tools + static_prompt]` — persists across every invocation of the same agent regardless of `task_context` / history / input.
2. `[tools + system + history + user input]` — within a single `agent.invoke()` only the scratchpad changes between tool calls, so every LLM call after the first in the loop reads this entire prefix from cache (~0.1× cost).

Invariants to preserve — breaking any of these silently invalidates the cache:

- Nothing volatile (timestamps, UUIDs, per-request IDs) in the static system block or in the tool schemas. Keep `task_context` in block 1 only.
- Tool order and tool schemas must be deterministic per agent (same `create_*_tools(...)` output across invocations).
- Don't collapse the two-block system back into one string — `_apply_anthropic_cache` depends on block 0 being the static half.
- The transform is gated on `LLM_PROVIDER=anthropic`; for Gemini the template falls back to a single-string system and the LLM call is unchanged.

Verify caching is working via `response.usage.cache_read_input_tokens` / `cache_creation_input_tokens` on LangChain's AIMessage `response_metadata` — if both stay zero across repeated requests, something in the prefix is drifting.

## Tools

Tools are wired in `tools/langchain_tools.py` via `create_read_tools`, `create_write_tools`, `create_python_tools`, `create_research_tools`. Sheet-mutating tools live under `tools/<feature>/`.

### Research tools (`tools/common_tools/common_tools.py`)
- `web_search(query, num_results)` — DuckDuckGo → Serper → Brave fallback chain.
- `web_search_financial(query, region, num_results, prefer_pdf)` — region-aware (us/in/auto), biases queries toward SEC, BSE/NSE/SEBI, IR pages.
- `fetch_url(url, max_chars, extract_tables)` — HTML (BeautifulSoup) and PDF (pdfplumber). For PDFs, also returns structured `tables` (list of 2-D arrays).
- `fetch_filing_us(ticker, form_type, limit)` — SEC EDGAR. Resolves ticker → CIK via cached `company_tickers.json`, then pulls the requested 10-K/10-Q/8-K and runs it through `fetch_url`.
- `fetch_filing_india(ticker, year)` — BSE annual reports. Resolves ticker → scripcode, fetches the AR list, picks year (or latest), pulls PDF. Falls back to `web_search_financial` if BSE blocks.

### Sheet tools
Mostly self-explanatory: `read_sheet_structure`, `read_range`, `get_chunk`, `write_values`, `apply_cell_format`, `add_conditional_format`, `create_chart`, `create_pivot_table`, `set_data_validation`, `create_named_range`, `create_sheet`, `audit_formulas`, `trace_dependents`. Plus a sandboxed Python tool family (`run_python`, `load_sheet_to_df`, `write_df_to_sheet`).

## No Test Framework

There is no automated test suite. Testing is done by deploying to GAS and running the add-on against a live Google Sheet. For backend-only smoke tests, use `python -c "from llm.agentFactory import getAgent; getAgent('planner')"` to verify imports.

### Code Guidelines
After doing changes in the code: update CLAUDE.md accordingly.
Codex will review your code
