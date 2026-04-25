import json
import logging
import os
import re
import time
from dataclasses import asdict

from flask import Blueprint, request, jsonify
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from llm.agentFactory import getAgent
from llm.llmFactory import get_llm

from utilities.task_context import TaskContext
from tools.read_structure.read_sheet_structure import read_sheet_structure

from tools.utils import build_sheets_service
from utils import load_access_token_from_file

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__)

# In-memory chat history keyed by spreadsheetId
_chat_histories: dict[str, list] = {}
# In-memory task contexts keyed by spreadsheetId
_task_contexts: dict[str, TaskContext] = {}

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


_STEP_DESC_MAX = 200   # chars per plan step description passed to execution/verifier


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


def _log_task_context(agent_type: str, tc: TaskContext, slim: bool = False) -> None:
    """Log the full serialized task_context being sent to an agent. Useful for debugging
    why an agent is making a decision (wrong sheet structure, stale plan, etc.)."""
    serialized = _serialize_task_context(tc, slim=slim)
    logger.info(
        "[TASK_CONTEXT → %s] status=%s iter=%d/%d sheet_id=%s plan_steps=%d serialized_len=%d",
        agent_type,
        tc.status,
        tc.current_iteration,
        tc.max_iteration,
        tc.active_sheet_id,
        len((tc.plan or {}).get("steps", [])),
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
    # P5: reload token on every request so an expired token doesn't stay stale
    # indefinitely. File read + service construction are cheap (no network call).
    access_token = load_access_token_from_file()
    sheet_service = build_sheets_service(access_token)

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
                task_context.sheet_structure = read_sheet_structure(sheet_service, spreadsheet_id)

                _log_task_context(agent_type, task_context)

                llm_start = time.time()
                raw_result = agent.invoke({
                    "task_context": _serialize_task_context(task_context),
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
                    plan = task_context.plan or {}
                    logger.info(f"[AGENT LOOP] Plan ready with {len(plan.get('steps', []))} steps")
                else:
                    request_history.append(AIMessage(content=raw_result.get("output", "")))
                    request_history.append(HumanMessage(content="Your last output was either not valid JSON or lacked a recognized 'status'. Please try again and ensure your response is a valid JSON block containing 'status' as either 'ready' or 'needs_clarification'."))
                    logger.info(f"[AGENT LOOP] {agent_type} returned invalid JSON, retrying...")
                    continue

            elif task_context.status == "executing":
                plan_steps = (task_context.plan or {}).get("steps", [])
                sorted_steps = _topo_sort(plan_steps)
                logger.info(f"[PHASE] EXECUTING - {len(sorted_steps)} steps to execute")

                completed_steps: list = []
                all_step_results: list = []
                sheets_modified_set: set = set()
                ranges_written_list: list = []
                step_failed = False

                task_context.sheet_structure = read_sheet_structure(sheet_service, spreadsheet_id)
                serialized_context = _serialize_task_context(task_context)

                _log_task_context("execution", task_context)

                for step in sorted_steps:
                    step_id = step.get("id", "?")
                    complexity = step.get("complexity", "low")
                    llm_type = effort if complexity == "high" else "fast"

                    logger.info(f"[EXECUTION] Step {step_id}: {step.get('description', '')[:80]}... (complexity: {complexity}, llm: {llm_type})")

                    step_input = json.dumps({
                        "step": step,
                        "completed_steps": completed_steps,
                    })
                    execution_agent = getAgent("execution", llm_type)
                    if llm_type == "fast":
                        stats["fast_agent_calls"] += 1
                    else:
                        stats["effort_agent_calls"] += 1

                    step_start = time.time()
                    tool_call_start = time.time()
                    try:
                        raw_result = execution_agent.invoke({
                            "task_context": serialized_context,
                            "input": step_input,
                        })
                        tool_call_time = time.time() - tool_call_start
                    except Exception as te:
                        tool_call_time = time.time() - tool_call_start
                        stats["tool_call_timings"].append({
                            "step_id": step_id,
                            "tool": "execution_agent",
                            "duration": tool_call_time,
                            "error": str(te),
                        })
                        if len(stats["failed_tool_calls"]) < 10:
                            stats["failed_tool_calls"].append({
                                "step_id": step_id,
                                "request": step_input[:500],
                                "exception": str(te),
                            })
                        raise

                    parsed = _parse_output(raw_result)

                    step_status = parsed.get("status", "failed")
                    evidence = parsed.get("evidence", "")

                    stats["tool_call_timings"].append({
                        "step_id": step_id,
                        "tool": "execution_agent",
                        "duration": tool_call_time,
                    })

                    all_step_results.append({
                        "id": step_id,
                        "status": step_status,
                        "evidence": evidence,
                    })
                    for sheet in parsed.get("sheets_modified", []):
                        sheets_modified_set.add(sheet)
                    ranges_written_list.extend(parsed.get("ranges_written", []))

                    if step_status == "failed":
                        logger.warning(f"[EXECUTION] Step {step_id} FAILED - skipping remaining steps")
                        step_failed = True
                        break

                    completed_steps.append({
                        "id": step_id,
                        "status": step_status,
                        "summary": evidence[:400],
                    })
                    logger.info(f"[EXECUTION] Step {step_id} completed in {time.time() - step_start:.2f}s")

                # Mark any steps that never ran as skipped.
                executed_ids = {r["id"] for r in all_step_results}
                for step in plan_steps:
                    if step["id"] not in executed_ids:
                        all_step_results.append({
                            "id": step["id"],
                            "status": "skipped",
                            "evidence": "Skipped — a prior step failed.",
                        })

                task_context.execution_report = {
                    "steps_executed": all_step_results,
                    "artifacts": {
                        "sheets_modified": list(sheets_modified_set),
                        "ranges_written": ranges_written_list,
                    },
                    "notes": "",
                }
                task_context.status = "replanning" if step_failed else "verifying"
                logger.info(f"[PHASE] Executing complete - {len(all_step_results)} steps executed, transitioning to {task_context.status}")

            elif task_context.status == "verifying":
                logger.info(f"[PHASE] VERIFYING")

                verifier_agent = getAgent("verifier", "fast")
                stats["verifier_calls"] += 1
                stats["fast_agent_calls"] += 1
                task_context.sheet_structure = read_sheet_structure(sheet_service, spreadsheet_id)

                _log_task_context("verifier", task_context)

                verify_start = time.time()
                raw_result = verifier_agent.invoke({
                    "task_context": _serialize_task_context(task_context),
                    "input": query,
                    "chat_history": request_history,
                })
                verify_time = time.time() - verify_start

                logger.info(f"[AGENT] Verifier call completed in {verify_time:.2f}s")

                parsed = _parse_output(raw_result)

                task_context.verification_report = parsed.get("verification_report", {})
                task_context.status = "replanning" if parsed.get("status") == "failed" else "completed"
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
        error_info = f" [ERROR: {tc.get('error', '')}]" if tc.get("error") else ""
        logger.info(f"[STATS] {i:2d}. {tc.get('step_id', '?'):30s} - {tc.get('duration', 0):6.2f}s{error_info}")
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
    _log_task_context("basic", task_context, slim=True)
    basic_start = time.time()
    result = basic_agent.invoke({
        "task_context": _serialize_task_context(task_context, slim=True),
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
