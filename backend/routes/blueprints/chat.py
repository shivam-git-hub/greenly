import json
import logging
import re
from dataclasses import asdict

from flask import Blueprint, request, jsonify
from langchain_core.messages import HumanMessage, AIMessage

from llm.agentFactory import getAgent

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

access_token = load_access_token_from_file()
sheet_service = build_sheets_service(access_token)

# ── History compaction ─────────────────────────────────────────────────────────
# Rough token budget for history. Each character ≈ 0.25 tokens; we keep a
# generous char budget so we stay well under the 30k input-token/min limit.
_HISTORY_MAX_CHARS = 12_000   # ~3k tokens of history
_HISTORY_KEEP_PAIRS = 2       # always keep the N most-recent human/AI pairs

def _compact_history(history: list) -> list:
    """
    Trim chat history when its total character length exceeds _HISTORY_MAX_CHARS.
    Always preserves the most recent _HISTORY_KEEP_PAIRS exchanges. Older
    messages are dropped from the front until the budget is met.
    """
    total = sum(len(str(m.content)) for m in history)
    if total <= _HISTORY_MAX_CHARS:
        return history

    # Pair messages up (human + AI) from the tail so we never split a pair.
    # history is [H, A, H, A, ...] — even indices are human, odd are AI.
    keep = history[-(2 * _HISTORY_KEEP_PAIRS):]   # guaranteed-keep tail
    candidates = history[:-(2 * _HISTORY_KEEP_PAIRS)]

    budget = _HISTORY_MAX_CHARS - sum(len(str(m.content)) for m in keep)
    retained = []
    for msg in reversed(candidates):
        msg_len = len(str(msg.content))
        if budget >= msg_len:
            retained.insert(0, msg)
            budget -= msg_len
        else:
            break   # stop as soon as we can't fit the next older message

    compacted = retained + keep
    dropped = len(history) - len(compacted)
    if dropped:
        logger.info("History compacted: dropped %d messages to stay under token budget", dropped)
    return compacted


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
    data = request.get_json()

    spreadsheet_id  = data.get("spreadsheetId", "")
    active_sheet    = data.get("activeSheet", "")
    active_sheet_id = data.get("activeSheetId")
    active_range    = data.get("activeRange", "")
    query           = data.get("query", "")
    mode            = data.get("mode", "ask")
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
        clarifying_question = task_context.clarifying_questions.pop(0)
        task_context.user_notes.append(f"{clarifying_question}\t{query}")
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
    task_context.sheet_structure = read_sheet_structure(sheet_service, spreadsheet_id)

    history = _chat_histories.setdefault(spreadsheet_id, [])

    # Main orchestration loop. current_iteration counts FULL planning cycles
    # (incremented only on entering planner / replanner). Inner state transitions
    # (executing → verifying) do not consume an iteration.

    task_context.max_iteration = 50
    exhausted = False
    pipeline_error: str = ""
    while task_context.status != "completed":
        try:
            if task_context.status in ("planning", "replanning"):
                if task_context.current_iteration >= task_context.max_iteration:
                    exhausted = True
                    break
                task_context.current_iteration += 1

                agent_type = "planner" if task_context.status == "planning" else "replanner"
                agent = getAgent(agent_type, effort)
                # Planner needs sheet_structure to inspect the spreadsheet.
                # Replanner only needs the failure reports — strip the large structure.
                slim = (agent_type == "replanner")
                raw_result = agent.invoke({
                    "task_context": _serialize_task_context(task_context, slim=slim),
                    "input": query,
                    "chat_history": _compact_history(history),
                })
                parsed = _parse_output(raw_result)

                if parsed.get("status") == "needs_clarification":
                    task_context.status = "awaiting_clarification"
                    task_context.clarifying_questions = parsed.get("clarifying_questions", [])
                    return jsonify({"response": "\n".join(task_context.clarifying_questions)})

                elif parsed.get("status") == "ready":
                    task_context.status = "executing"
                    task_context.plan = parsed.get("plan", {})
                    task_context.execution_report = {}
                    task_context.verification_report = {}
                else:
                    history.append(AIMessage(content=raw_result.get("output", "")))
                    history.append(HumanMessage(content="Your last output was either not valid JSON or lacked a recognized 'status'. Please try again and ensure your response is a valid JSON block containing 'status' as either 'ready' or 'needs_clarification'."))
                    continue

            elif task_context.status == "executing":
                execution_agent = getAgent("execution", effort)
                raw_result = execution_agent.invoke({
                    "task_context": _serialize_task_context(task_context, slim=True),
                    "input": query,
                    "chat_history": _compact_history(history),
                })
                parsed = _parse_output(raw_result)

                task_context.execution_report = parsed.get("execution_report", {})
                task_context.status = "replanning" if parsed.get("status") == "failed" else "verifying"

            elif task_context.status == "verifying":
                verifier_agent = getAgent("verifier", effort)
                raw_result = verifier_agent.invoke({
                    "task_context": _serialize_task_context(task_context, slim=True),
                    "input": query,
                    "chat_history": _compact_history(history),
                })
                parsed = _parse_output(raw_result)

                task_context.verification_report = parsed.get("verification_report", {})
                task_context.status = "replanning" if parsed.get("status") == "failed" else "completed"

            else:
                break

        except Exception as e:
            # All LLM retries exhausted or an unexpected error — bail gracefully
            # so the basic agent can still write a user-facing error message.
            logger.error("Pipeline error at status=%s: %s", task_context.status, e)
            pipeline_error = str(e)
            exhausted = True
            break

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
    result = basic_agent.invoke({
        "task_context": _serialize_task_context(task_context, slim=True),
        "input": query,
        "system_prompt": basic_system_prompt,
        "chat_history": _compact_history(history),
    })
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

    history.append(HumanMessage(content=query))
    history.append(AIMessage(content=output))

    return jsonify({"response": output})
