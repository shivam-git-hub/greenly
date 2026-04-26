import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from flask import Blueprint, request, jsonify
from langchain.agents import AgentExecutor
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from llm.agentFactory import getAgent
from llm.llmFactory import get_llm

from utilities.task_context import TaskContext
from tools.read_structure.read_sheet_structure import read_sheet_structure

from tools.utils import build_sheets_service
from tools.langchain_tools import create_read_tools, create_write_tools, create_python_tools
from utils import load_access_token_from_file

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__)

# In-memory chat history keyed by spreadsheetId
_chat_histories: dict[str, list] = {}
# In-memory task contexts keyed by spreadsheetId
_task_contexts: dict[str, TaskContext] = {}
# Cached Sheets service — rebuilt only on first request; avoids file I/O +
# HTTP client construction on every POST /api/v1/chat.
_sheet_service = None


def _get_sheet_service():
    global _sheet_service
    if _sheet_service is None:
        _sheet_service = build_sheets_service(load_access_token_from_file())
    return _sheet_service


# ── History compaction ─────────────────────────────────────────────────────────
_HISTORY_MAX_CHARS  = 12_000  # ~3k tokens — trigger summarisation above this
_HISTORY_KEEP_PAIRS = 2       # always keep N most-recent H/A pairs verbatim

_MAX_ITERATIONS        = int(os.getenv("PIPELINE_MAX_ITERATIONS", "50"))
_MAX_EXCEPTION_RETRIES = int(os.getenv("PIPELINE_MAX_EXCEPTION_RETRIES", "5"))

_SUMMARY_SYSTEM_PROMPT = (
    "You are a conversation memory compactor for an AI spreadsheet assistant. "
    "Given prior exchanges, write a concise summary (3-5 bullet points) covering: "
    "what the user asked for, what was accomplished or attempted, and any key context "
    "useful for answering future questions about the same spreadsheet. "
    "Be factual and brief. No preamble."
)


def _summarize_history(messages: list) -> str:
    """Call the fast LLM to summarise older HumanMessage/AIMessage objects."""
    lines = []
    for msg in messages:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        content = str(msg.content)
        if len(content) > 2000:
            lines.append(f"{role}: {content[:2000]} …[truncated, {len(content)} chars total]")
        else:
            lines.append(f"{role}: {content}")
    conversation_text = "\n\n".join(lines)
    try:
        response = get_llm("fast").invoke([
            SystemMessage(content=_SUMMARY_SYSTEM_PROMPT),
            HumanMessage(content=f"Summarise these exchanges:\n\n{conversation_text}"),
        ])
        return str(response.content)
    except Exception as e:
        logger.warning("History summarisation failed (%s); falling back to truncation", e)
        return "\n".join(f"- {l[:120]}" for l in lines[:6])


def _ensure_compact_history(history: list) -> list:
    """
    Compact chat history in-place when total chars exceed _HISTORY_MAX_CHARS.

    Keeps the last _HISTORY_KEEP_PAIRS H/A pairs verbatim. All older messages
    are replaced with a single LLM-generated summary AIMessage. Modifies the
    list in-place so the stored history stays bounded across requests.
    Called once per HTTP request before the pipeline loop starts.
    """
    total = sum(len(str(m.content)) for m in history)
    if total <= _HISTORY_MAX_CHARS:
        return history

    keep = history[-(2 * _HISTORY_KEEP_PAIRS):]
    to_summarize = history[:-(2 * _HISTORY_KEEP_PAIRS)]
    if not to_summarize:
        return history

    summary_text = _summarize_history(to_summarize)
    summary_msg = AIMessage(
        content=f"[Summary of {len(to_summarize)} earlier messages]\n{summary_text}"
    )

    history[:] = [summary_msg] + keep
    logger.info(
        "History summarised: condensed %d messages into summary + %d recent",
        len(to_summarize), len(keep),
    )
    return history


_STEP_DESC_MAX       = 200   # chars per plan step description passed to execution/verifier
_REPLAN_EVIDENCE_MAX = 500   # chars per step evidence sent to replanner


def _serialize_task_context(tc: TaskContext, slim: bool = False) -> str:
    """
    JSON-serialize the TaskContext for injection into agent system prompts.

    slim=True (used for execution, verifier, replanner, basic):
      - Drops sheet_structure — agents read it directly via tools when needed.
      - Truncates each plan step description to _STEP_DESC_MAX chars to avoid
        oversized inputs when the planner wrote very verbose step descriptions.
    """
    d = asdict(tc)
    if slim:
        d.pop("sheet_structure", None)
        plan = d.get("plan") or {}
        for step in plan.get("steps") or []:
            if len(step.get("description", "")) > _STEP_DESC_MAX:
                step["description"] = step["description"][:_STEP_DESC_MAX] + "…"
    return json.dumps(d, default=str, indent=2)


def _slim_sheet_structure(ss: dict | None) -> dict | None:
    """Strip dataBlocks from sheet_structure — keeps sheet names/usedRange/namedRanges for navigation."""
    if not ss:
        return None
    return {
        "spreadsheetId": ss.get("spreadsheetId"),
        "title":         ss.get("title"),
        "namedRanges":   ss.get("namedRanges", []),
        "sheets": [
            {k: v for k, v in s.items() if k != "dataBlocks"}
            for s in ss.get("sheets", [])
        ],
    }


def _ctx_planner(tc: TaskContext) -> str:
    return json.dumps({
        "user_request":      tc.user_request,
        "spreadsheet_id":    tc.spreadsheet_id,
        "active_sheet_id":   tc.active_sheet_id,
        "sheet_structure":   tc.sheet_structure,
        "user_notes":        tc.user_notes,
        "current_iteration": tc.current_iteration,
    }, default=str, indent=2)


def _ctx_replanner(tc: TaskContext) -> str:
    plan = tc.plan or {}
    chains = plan.get("chains") or []
    if not chains and plan.get("steps"):
        chains = [{"id": "C1", "steps": plan["steps"]}]
    condensed_chains = [
        {**chain, "steps": [
            {**s, "description": s["description"][:150] + "…"
                   if len(s.get("description", "")) > 150 else s.get("description", "")}
            for s in chain.get("steps", [])
        ]}
        for chain in chains
    ]
    exec_report = tc.execution_report or {}
    condensed_exec = [
        {"id": s["id"], "status": s["status"],
         "evidence": s.get("evidence", "")[:_REPLAN_EVIDENCE_MAX]}
        for s in exec_report.get("steps_executed", [])
    ]
    plan_out = {k: v for k, v in plan.items() if k not in ("chains", "steps")}
    plan_out["chains"] = condensed_chains
    return json.dumps({
        "user_request":      tc.user_request,
        "spreadsheet_id":    tc.spreadsheet_id,
        "active_sheet_id":   tc.active_sheet_id,
        "sheet_structure":   tc.sheet_structure,
        "user_notes":        tc.user_notes,
        "current_iteration": tc.current_iteration,
        "plan":              plan_out,
        "execution_report":  {**exec_report, "steps_executed": condensed_exec},
        "verification_report": tc.verification_report,
    }, default=str, indent=2)


def _ctx_execution(tc: TaskContext) -> str:
    plan = tc.plan or {}
    return json.dumps({
        "user_request":    tc.user_request,
        "spreadsheet_id":  tc.spreadsheet_id,
        "active_sheet_id": tc.active_sheet_id,
        "sheet_structure": tc.sheet_structure,
        "plan": {
            "goal":                  plan.get("goal", ""),
            "verification_criteria": plan.get("verification_criteria", []),
        },
    }, default=str, indent=2)


def _compact_step_input(step: dict, completed_steps: list) -> str:
    # Strip complexity (orchestrator-only). completed is just IDs — step descriptions
    # are self-contained in each step's own input; only successful steps are listed.
    step_clean = {k: v for k, v in step.items() if k != "complexity"}
    completed_ids = [s["id"] for s in completed_steps]
    return json.dumps({"step": step_clean, "completed": completed_ids})


def _ctx_verifier(tc: TaskContext) -> str:
    plan = tc.plan or {}
    exec_report = tc.execution_report or {}
    condensed_steps = [
        {"id": s["id"], "status": s["status"]}
        for s in exec_report.get("steps_executed", [])
    ]
    return json.dumps({
        "user_request":    tc.user_request,
        "spreadsheet_id":  tc.spreadsheet_id,
        "active_sheet_id": tc.active_sheet_id,
        "sheet_structure": _slim_sheet_structure(tc.sheet_structure),
        "plan": {
            "goal":                  plan.get("goal", ""),
            "verification_criteria": plan.get("verification_criteria", []),
        },
        "execution_report": {
            "steps_executed": condensed_steps,
            "artifacts":      exec_report.get("artifacts", {}),
            "notes":          exec_report.get("notes", ""),
        },
        "current_iteration": tc.current_iteration,
    }, default=str, indent=2)


def _ctx_basic(tc: TaskContext) -> str:
    plan = tc.plan or {}
    exec_report = tc.execution_report or {}
    condensed_steps = [
        {"id": s["id"], "status": s["status"],
         "summary": s.get("evidence", "")[:120]}
        for s in exec_report.get("steps_executed", [])
    ]
    return json.dumps({
        "user_request": tc.user_request,
        "status":       tc.status,
        "plan":         {"goal": plan.get("goal", "")},
        "execution_report": {
            "steps_executed": condensed_steps,
            "artifacts":      exec_report.get("artifacts", {}),
        },
        "verification_report": tc.verification_report,
    }, default=str, indent=2)


def _topo_sort(steps: list) -> list:
    """Return plan steps ordered so every step comes after all its dependencies."""
    id_to_step = {s["id"]: s for s in steps}
    visited: set = set()
    result: list = []

    def visit(step_id: str):
        if step_id in visited or step_id not in id_to_step:
            return
        visited.add(step_id)
        for dep in id_to_step[step_id].get("depends_on", []):
            visit(dep)
        result.append(id_to_step[step_id])

    for step in steps:
        visit(step["id"])

    return result


def _get_chains(plan: dict) -> list:
    """Return chains from plan. Falls back to a single chain for legacy flat steps[] plans."""
    chains = plan.get("chains") or []
    if chains:
        return chains
    steps = plan.get("steps") or []
    if steps:
        logger.warning("[PLAN] Legacy flat steps[] format detected — wrapping in single chain")
        return [{"id": "C1", "steps": steps}]
    return []


def _log_task_context(agent_type: str, serialized: str, tc: TaskContext) -> None:
    """Log exactly what task_context the agent receives."""
    chains = _get_chains(tc.plan or {})
    total_steps = sum(len(c.get("steps", [])) for c in chains)
    logger.info(
        "[TASK_CONTEXT → %s] status=%s iter=%d/%d sheet_id=%s chains=%d steps=%d ctx_len=%d",
        agent_type,
        tc.status,
        tc.current_iteration,
        tc.max_iteration,
        tc.active_sheet_id,
        len(chains),
        total_steps,
        len(serialized),
    )
    logger.info("[TASK_CONTEXT → %s body]\n%s", agent_type, serialized)


def _parse_output(raw_result: dict) -> dict:
    out = raw_result.get("output", "{}")
    if isinstance(out, list):
        out = next((p.get("text", "") for p in out if isinstance(p, dict) and "text" in p), "{}")
    match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', out, re.DOTALL)
    if match:
        out = match.group(1)
    try:
        return json.loads(out)
    except Exception:
        return {"status": "failed"}



@chat_bp.route("/api/v1/chat", methods=["POST"])
def chat():
    sheet_service = _get_sheet_service()

    data = request.get_json()

    spreadsheet_id  = data.get("spreadsheetId", "")
    active_sheet_id = data.get("activeSheetId")
    query           = data.get("query", "")
    effort          = data.get("effort", "fast")

    if not query:
        return jsonify({"error": "query is required"}), 400

    # Retrieve or initialize TaskContext for this session/spreadsheet
    if spreadsheet_id not in _task_contexts:
        _task_contexts[spreadsheet_id] = TaskContext(
            user_request=query,
            spreadsheet_id=spreadsheet_id,
            active_sheet_id=active_sheet_id,
        )
    task_context = _task_contexts[spreadsheet_id]

    if task_context.status in ("executing", "verifying"):
        return jsonify({"error": "Task is already executing or verifying"}), 400

    if task_context.status == "awaiting_clarification":
        # All questions were shown to the user at once; record them all against
        # the single reply so the planner sees every Q→A pair in user_notes.
        if task_context.clarifying_questions:
            combined_questions = " | ".join(task_context.clarifying_questions)
            task_context.user_notes.append(f"{combined_questions}\t{query}")
            task_context.clarifying_questions = []
        task_context.status = "planning"

    if task_context.status in ("completed", "failed"):
        task_context.status = "planning"
        task_context.current_iteration = 0
        task_context.plan = {}
        task_context.execution_report = {}
        task_context.verification_report = {}

    # Update context with current request details
    task_context.user_request = query
    task_context.active_sheet_id = active_sheet_id

    history = _chat_histories.setdefault(spreadsheet_id, [])
    # Compact the persistent history once before the pipeline starts.
    _ensure_compact_history(history)

    # B1/E1: use a per-request copy for in-loop chat_history. Planner retry
    # messages (bad JSON output + corrective prompt) are appended here so the
    # next planner call sees them, but they never pollute the persistent history.
    request_history = list(history)

    # ── Statistics tracking ─────────────────────────────────────────────────────
    stats = {
        "total_tool_calls": 0,
        "total_iterations": 0,
        "executor_calls": 0,
        "verifier_calls": 0,
        "planner_calls": 0,
        "replanner_calls": 0,
        "fast_agent_calls": 0,
        "effort_agent_calls": 0,
        "tool_call_timings": [],
        "failed_tool_calls": [],
    }
    phase_start_time = time.time()

    # Main orchestration loop. current_iteration counts FULL planning cycles
    # (incremented only on entering planner / replanner). Inner state transitions
    # (executing → verifying) do not consume an iteration.

    task_context.max_iteration = _MAX_ITERATIONS
    exhausted = False
    pipeline_error: str = ""
    exception_retries = 0

    logger.info(f"[AGENT LOOP] Starting agent loop for query: {query[:100]}...")
    logger.info(f"[AGENT LOOP] Initial status: {task_context.status}, effort: {effort}")

    while task_context.status != "completed":
        try:
            
            if task_context.status in ("planning", "replanning"):
                logger.info(f"[PHASE] {task_context.status.upper()} - Iteration {task_context.current_iteration}/{task_context.max_iteration}")

                if task_context.current_iteration >= task_context.max_iteration:
                    exhausted = True
                    logger.warning(f"[AGENT LOOP] Max iterations reached: {task_context.max_iteration}")
                    break
                task_context.current_iteration += 1

                agent_type = "planner" if task_context.status == "planning" else "replanner"
                if agent_type == "planner":
                    stats["planner_calls"] += 1
                else:
                    stats["replanner_calls"] += 1
                agent = getAgent(agent_type, effort)
                logger.info(f"[AGENT CALL] Starting {agent_type.upper()} agent with effort={effort}")
                task_context.sheet_structure = read_sheet_structure(sheet_service, spreadsheet_id)

                ctx_fn = _ctx_planner if agent_type == "planner" else _ctx_replanner
                agent_ctx = ctx_fn(task_context)
                _log_task_context(agent_type, agent_ctx, task_context)

                llm_start = time.time()
                logger.info(f"[AGENT CALL] Invoking {agent_type.upper()} agent")
                raw_result = agent.invoke({
                    "task_context": agent_ctx,
                    "input": query,
                    "chat_history": request_history,
                })

                stats["total_iterations"] += 1
                if effort == "fast":
                    stats["fast_agent_calls"] += 1
                else:
                    stats["effort_agent_calls"] += 1

                logger.info(f"[AGENT] {agent_type} call completed in {time.time() - llm_start:.2f}s")

                parsed = _parse_output(raw_result)

                if parsed.get("status") == "needs_clarification":
                    task_context.status = "awaiting_clarification"
                    task_context.clarifying_questions = parsed.get("clarifying_questions", [])
                    logger.info(f"[AGENT LOOP] Requesting clarification: {task_context.clarifying_questions}")
                    return jsonify({"response": "\n".join(task_context.clarifying_questions)})

                elif parsed.get("status") == "ready":
                    task_context.status = "executing"
                    task_context.plan = parsed.get("plan", {})
                    task_context.execution_report = {}
                    task_context.verification_report = {}
                    chains = _get_chains(task_context.plan or {})
                    total_steps = sum(len(c.get("steps", [])) for c in chains)
                    logger.info(f"[AGENT LOOP] Plan ready — {len(chains)} chain(s), {total_steps} step(s)")
                else:
                    request_history.append(AIMessage(content=raw_result.get("output", "")))
                    request_history.append(HumanMessage(content="Your last output was either not valid JSON or lacked a recognized 'status'. Please try again and ensure your response is a valid JSON block containing 'status' as either 'ready' or 'needs_clarification'."))
                    logger.info(f"[AGENT LOOP] {agent_type} returned invalid JSON, retrying...")
                    continue

            elif task_context.status == "executing":
                chains = _get_chains(task_context.plan or {})
                total_steps = sum(len(c.get("steps", [])) for c in chains)
                logger.info(f"[PHASE] EXECUTING — {len(chains)} chain(s), {total_steps} total steps")

                task_context.sheet_structure = read_sheet_structure(sheet_service, spreadsheet_id)
                serialized_context = _ctx_execution(task_context)

                # Get fresh LLMAgents for creating per-chain isolated services
                from llm.agentFactory import LLMAgents
                llmAgents = LLMAgents("fast")
                access_token = load_access_token_from_file()

                def run_chain(chain: dict, access_token: str, llmAgents) -> dict:
                    # Create fresh service per chain to avoid SSL conflicts in parallel execution
                    from tools.utils import build_sheets_service
                    chain_service = build_sheets_service(access_token)
                    chain_read_tools = create_read_tools(chain_service)
                    chain_write_tools = create_write_tools(chain_service)
                    chain_python_tools = create_python_tools(chain_service)

                    # Build execution tools with chain's isolated service
                    execution_tools = (
                        chain_write_tools +
                        chain_read_tools +
                        chain_python_tools +
                        [llmAgents.researcher_tool]
                    )

                    # Create fresh AgentExecutor per chain for thread safety
                    execution_agent_executor = AgentExecutor(
                        agent=llmAgents.executionAgent,
                        tools=execution_tools,
                        max_iterations=60,
                        verbose=True,
                        handle_parsing_errors=True,
                    )

                    chain_id = chain.get("id", "?")
                    chain_steps = _topo_sort(chain.get("steps", []))
                    completed: list = []
                    results: list = []
                    sheets_mod: set = set()
                    ranges_wr: list = []
                    timings: list = []
                    failed_calls_local: list = []
                    fast_calls = 0
                    effort_calls = 0
                    chain_failed = False

                    for step in chain_steps:
                        step_id   = step.get("id", "?")
                        llm_type  = effort if step.get("complexity", "low") == "high" else "fast"
                        logger.info(f"[CHAIN {chain_id}] Step {step_id}: {step.get('description','')[:80]}... ({llm_type})")

                        step_input = _compact_step_input(step, completed)
                        step_description = step.get('description', '')[:80]
                        logger.info(f"[AGENT CALL] Starting EXECUTION agent chain={chain_id} step={step_id} effort={llm_type}")
                        if llm_type == "fast":
                            fast_calls += 1
                        else:
                            effort_calls += 1

                        t0 = time.time()
                        try:
                            logger.info(f"[AGENT CALL] Invoking EXECUTION agent chain={chain_id} step={step_id}")
                            raw_result = execution_agent_executor.invoke({
                                "task_context": serialized_context,
                                "input": step_input,
                            })
                            duration = time.time() - t0
                        except Exception as e:
                            duration = time.time() - t0
                            timings.append({"step_id": step_id, "step_description": step_description, "tool": "execution_agent_executor", "duration": duration, "error": str(e)})
                            failed_calls_local.append({"step_id": step_id, "request": step_input[:500], "exception": str(e)})
                            results.append({"id": step_id, "status": "failed", "evidence": f"Exception: {str(e)[:300]}"})
                            chain_failed = True
                            logger.error(f"[CHAIN {chain_id}] Step {step_id} exception: {e}")
                            break

                        parsed      = _parse_output(raw_result)
                        step_status = parsed.get("status", "failed")
                        evidence    = parsed.get("evidence", "")

                        timings.append({"step_id": step_id, "step_description": step_description, "tool": "execution_agent_executor", "duration": duration})
                        results.append({"id": step_id, "status": step_status, "evidence": evidence})
                        for sheet in parsed.get("sheets_modified", []):
                            sheets_mod.add(sheet)
                        ranges_wr.extend(parsed.get("ranges_written", []))

                        if step_status == "failed":
                            logger.warning(f"[CHAIN {chain_id}] Step {step_id} FAILED — stopping chain")
                            chain_failed = True
                            break

                        completed.append({"id": step_id, "status": step_status, "summary": evidence[:400]})
                        logger.info(f"[CHAIN {chain_id}] Step {step_id} done in {duration:.2f}s")

                    executed_ids = {r["id"] for r in results}
                    for step in chain_steps:
                        if step["id"] not in executed_ids:
                            results.append({"id": step["id"], "status": "skipped", "evidence": "Skipped — a prior step in this chain failed."})

                    return {
                        "results":       results,
                        "sheets_modified": sheets_mod,
                        "ranges_written":  ranges_wr,
                        "failed":        chain_failed,
                        "fast_calls":    fast_calls,
                        "effort_calls":  effort_calls,
                        "timings":       timings,
                        "failed_calls":  failed_calls_local,
                    }

                all_step_results:   list = []
                sheets_modified_set: set = set()
                ranges_written_list: list = []
                any_chain_failed = False

                # Run chains in parallel with per-chain isolated services
                with ThreadPoolExecutor(max_workers=len(chains)) as executor:
                    futures = {
                        executor.submit(run_chain, chain, access_token, llmAgents): chain.get("id", "?")
                        for chain in chains
                    }
                    for future in as_completed(futures):
                        chain_id = futures[future]
                        try:
                            cr = future.result()
                            all_step_results.extend(cr["results"])
                            sheets_modified_set.update(cr["sheets_modified"])
                            ranges_written_list.extend(cr["ranges_written"])
                            if cr["failed"]:
                                any_chain_failed = True
                            stats["fast_agent_calls"]  += cr["fast_calls"]
                            stats["effort_agent_calls"] += cr["effort_calls"]
                            stats["tool_call_timings"].extend(cr["timings"])
                            cap = max(0, 10 - len(stats["failed_tool_calls"]))
                            stats["failed_tool_calls"].extend(cr["failed_calls"][:cap])
                        except Exception as e:
                            logger.error(f"[CHAIN {chain_id}] Unexpected error: {e}")
                            any_chain_failed = True
                            all_step_results.append({"id": f"chain_{chain_id}_error", "status": "failed", "evidence": f"Chain error: {str(e)[:300]}"})

                task_context.execution_report = {
                    "steps_executed": all_step_results,
                    "artifacts": {
                        "sheets_modified": list(sheets_modified_set),
                        "ranges_written":  ranges_written_list,
                    },
                    "notes": "",
                }
                task_context.status = "replanning" if any_chain_failed else "verifying"
                logger.info(f"[PHASE] Executing complete — {len(all_step_results)} steps across {len(chains)} chain(s), transitioning to {task_context.status}")

            elif task_context.status == "verifying":
                logger.info(f"[PHASE] VERIFYING")

                verifier_agent = getAgent("verifier", "fast")
                logger.info(f"[AGENT CALL] Starting VERIFIER agent with effort=fast")
                stats["verifier_calls"] += 1
                stats["fast_agent_calls"] += 1
                task_context.sheet_structure = read_sheet_structure(sheet_service, spreadsheet_id)

                verifier_ctx = _ctx_verifier(task_context)
                _log_task_context("verifier", verifier_ctx, task_context)

                verify_start = time.time()
                logger.info(f"[AGENT CALL] Invoking VERIFIER agent")
                raw_result = verifier_agent.invoke({
                    "task_context": verifier_ctx,
                    "input": query,
                    "chat_history": request_history,
                })
                verify_time = time.time() - verify_start

                logger.info(f"[AGENT] Verifier call completed in {verify_time:.2f}s")
                logger.info(f"[VERIFIER] Raw output preview: {str(raw_result)[:1000]}")

                parsed = _parse_output(raw_result)

                # Fallback logic: handle empty/incorrect verifier output
                # If verifier output is invalid (status != "failed" means it couldn't parse properly),
                # check execution_report to decide
                verifier_status = parsed.get("status")
                verification_report = parsed.get("verification_report", {})

                if not verification_report or verifier_status not in ("completed", "failed"):
                    # Verifier gave invalid/empty output - use execution_report as fallback
                    logger.warning(f"[VERIFIER] Invalid/empty output: status={verifier_status}, using execution_report fallback")
                    exec_report = task_context.execution_report or {}
                    steps = exec_report.get("steps_executed", [])

                    # Check if all steps succeeded
                    all_passed = all(s.get("status") == "success" for s in steps) if steps else False

                    if all_passed and steps:
                        # All steps passed - consider verification passed
                        verification_report = {
                            "verdict": "pass",
                            "criteria_results": [{"criterion": "All executed steps passed", "result": "pass", "evidence": "Fallback: execution report shows all steps succeeded"}],
                            "remediation_hints": []
                        }
                        verifier_status = "completed"
                    else:
                        # Some steps failed or no execution - go to replanning
                        verification_report = {
                            "verdict": "fail",
                            "criteria_results": [{"criterion": "Verifier could not run", "result": "fail", "evidence": "Fallback: verifier failed to produce output"}],
                            "remediation_hints": ["Re-run execution to generate proper results"]
                        }
                        verifier_status = "failed"

                task_context.verification_report = verification_report
                task_context.status = "replanning" if verifier_status == "failed" else "completed"
                logger.info(f"[PHASE] Verification {'FAILED - will replan' if task_context.status == 'replanning' else 'PASSED - completed'}")

            else:
                logger.error(f"recieved task_context.status {task_context.status}")
                exhausted = True
                pipeline_error = f"Unexpected pipeline status: {task_context.status}"
                break


        except Exception as e:
            logger.error("Pipeline error at status=%s: %s", task_context.status, e)
            exception_retries += 1
            if exception_retries >= _MAX_EXCEPTION_RETRIES:
                pipeline_error = str(e)
                exhausted = True
                break
            if task_context.status == "executing":
                # Execution exceptions go to replanning so the replanner sees the
                # error in history and can produce a corrected plan.
                task_context.status = "replanning"
                task_context.execution_report = {
                    "steps_executed": [],
                    "artifacts": {"sheets_modified": [], "ranges_written": []},
                    "notes": f"Execution aborted due to exception: {str(e)[:300]}",
                }
                request_history.append(AIMessage(content=f"Execution failed with exception: {str(e)}"))
                request_history.append(HumanMessage(content="Execution crashed. Replan and avoid whatever caused this error."))
                logger.warning("[EXCEPTION] Execution crash → replanning (retry %d/%d): %s", exception_retries, _MAX_EXCEPTION_RETRIES, str(e)[:200])
            else:
                request_history.append(AIMessage(content=f"Exception occurred: {str(e)}"))
                request_history.append(HumanMessage(content="An exception occurred. Please analyze the error and retry."))
                logger.warning("[EXCEPTION] Retry %d/%d at %s: %s", exception_retries, _MAX_EXCEPTION_RETRIES, task_context.status, str(e)[:200])
            continue

    total_loop_time = time.time() - phase_start_time

    stats["executor_calls"] = len([t for t in stats["tool_call_timings"] if t.get("tool") == "execution_agent"])
    stats["total_tool_calls"] = len(stats["tool_call_timings"])

    logger.info("=" * 60)
    logger.info("[AGENT LOOP] ========== EXECUTION STATISTICS ==========")
    logger.info(f"[STATS] Total Tool Calls:       {stats['total_tool_calls']}")
    logger.info(f"[STATS] Total Iterations:       {stats['total_iterations']}")
    logger.info(f"[STATS] Executor Calls:         {stats['executor_calls']}")
    logger.info(f"[STATS] Planner Calls:          {stats['planner_calls']}")
    logger.info(f"[STATS] Replanner Calls:        {stats['replanner_calls']}")
    logger.info(f"[STATS] Verifier Calls:         {stats['verifier_calls']}")
    logger.info(f"[STATS] Fast Agent Calls:       {stats['fast_agent_calls']}")
    logger.info(f"[STATS] Effort Agent Calls:     {stats['effort_agent_calls']}")
    logger.info(f"[STATS] Exception Retries:      {exception_retries}")
    logger.info(f"[STATS] Total Loop Time:        {total_loop_time:.2f}s")
    logger.info("-" * 60)
    logger.info("[STATS] ========== TOP 10 SLOWEST TOOL CALLS ==========")
    sorted_timings = sorted(stats["tool_call_timings"], key=lambda x: x.get("duration", 0), reverse=True)[:10]
    for i, tc in enumerate(sorted_timings, 1):
        error_info = f" [ERROR: {tc.get('error', '')[:50]}]" if tc.get("error") else ""
        step_desc = tc.get('step_description', tc.get('step_id', '?'))[:60]
        logger.info(f"[STATS] {i:2d}. {step_desc:60s} - {tc.get('duration', 0):6.2f}s{error_info}")
    logger.info("-" * 60)
    logger.info("[STATS] ========== FAILED TOOL CALLS (max 10) ==========")
    if stats["failed_tool_calls"]:
        for i, fc in enumerate(stats["failed_tool_calls"], 1):
            logger.info(f"[STATS] {i}. Step: {fc.get('step_id', '?')}")
            logger.info(f"[STATS]    Request: {fc.get('request', '')[:200]}...")
            logger.info(f"[STATS]    Exception: {fc.get('exception', '')[:200]}")
    else:
        logger.info("[STATS] No failed tool calls")
    logger.info("=" * 60)

    final_status = "failed" if exhausted else "completed"
    task_context.status = final_status

    if not exhausted:
        basic_system_prompt = (
            "The pipeline finished. Read task_context.status, plan, execution_report, and "
            "verification_report and write the final user-facing message according to your rules."
        )
    elif pipeline_error:
        basic_system_prompt = (
            f"The pipeline was interrupted by an unrecoverable error: {pipeline_error[:300]}. "
            "Write a brief, honest user-facing message explaining that the task could not be "
            "completed due to a technical issue, what was attempted so far (if anything), and "
            "that the user may try again."
        )
    else:
        basic_system_prompt = (
            "The pipeline ran out of retries before producing a verified result. Read "
            "task_context.execution_report and verification_report and write a brief, honest "
            "user-facing message explaining what was attempted, what blocked it, and (if obvious) "
            "what the user could change to unblock it."
        )

    basic_agent = getAgent("basic", effort)
    logger.info(f"[AGENT CALL] Starting BASIC agent with effort={effort}")
    basic_ctx = _ctx_basic(task_context)
    _log_task_context("basic", basic_ctx, task_context)
    basic_start = time.time()
    logger.info(f"[AGENT CALL] Invoking BASIC agent")
    result = basic_agent.invoke({
        "task_context": basic_ctx,
        "input": query,
        "system_prompt": basic_system_prompt,
        "chat_history": request_history,
    })
    logger.info(f"[AGENT] Basic agent call completed in {time.time() - basic_start:.2f}s")
    output = result.get("output", "")
    if isinstance(output, list):
        output = next((p.get("text", "") for p in output if isinstance(p, dict) and "text" in p), "")

    # Reset session state for the next request
    task_context.plan = {}
    task_context.execution_report = {}
    task_context.verification_report = {}
    task_context.clarifying_questions = []
    task_context.user_notes = []
    task_context.current_iteration = 0
    task_context.status = "completed"

    # Only the final user query + final answer go into persistent history
    history.append(HumanMessage(content=query))
    history.append(AIMessage(content=output))

    return jsonify({"response": output})
