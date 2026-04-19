import json
import re
from flask import Blueprint, request, jsonify
from langchain_core.messages import HumanMessage, AIMessage

from llm.agentFactory import getAgent

from utilities.task_context import TaskContext
from tools.read_structure.read_sheet_structure import read_sheet_structure

from tools.utils import build_sheets_service
from utils import load_access_token_from_file

chat_bp = Blueprint("chat", __name__)

# In-memory chat history keyed by spreadsheetId
_chat_histories: dict[str, list] = {}
# In-memory task contexts keyed by spreadsheetId
_task_contexts: dict[str, TaskContext] = {}

access_token = load_access_token_from_file()
sheet_service = build_sheets_service(access_token)

@chat_bp.route("/api/v1/chat", methods=["POST"])
def chat():
    data = request.get_json()

    spreadsheet_id = data.get("spreadsheetId", "")
    active_sheet   = data.get("activeSheet", "")
    active_sheet_id = data.get("activeSheetId")
    active_range   = data.get("activeRange", "")
    query          = data.get("query", "")
    mode           = data.get("mode", "ask")
    effort         = data.get("effort", "fast")
    access_token   = data.get("accessToken")

    if not query:
        return jsonify({"error": "query is required"}), 400

    # Retrieve or initialize TaskContext for this session/spreadsheet
    if spreadsheet_id not in _task_contexts:
        _task_contexts[spreadsheet_id] = TaskContext(
            user_request=query,
            spreadsheet_id=spreadsheet_id,
            active_sheet_id=active_sheet_id
        )
    task_context = _task_contexts[spreadsheet_id]

    if task_context.status == "executing" or task_context.status == "verifying":
        return jsonify({"error": "Task is already executing or verifying"}), 400

    if task_context.status == "awaiting_clarification":
        clarifying_question = task_context.clarifying_questions.pop(0)
        task_context.user_notes.append(f"{clarifying_question}\t{query}")
        task_context.status = "planning"
    
    if task_context.status == "completed":
        task_context.status = "planning"
    
    # Update context with current request details
    task_context.user_request = query
    task_context.active_sheet_id = active_sheet_id
    task_context.sheet_structure = read_sheet_structure(sheet_service, spreadsheet_id)
    
    # Retrieve or initialise per-spreadsheet chat history
    history = _chat_histories.setdefault(spreadsheet_id, [])
    
    
    def parse_output(raw_result):
        out = raw_result.get("output", "{}")
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', out, re.DOTALL)
        if match:
            out = match.group(1)
        try:
            return json.loads(out)
        except Exception:
            return {"status": "failed"}

    while task_context.status != "completed" and task_context.current_iteration < task_context.max_iteration:
        task_context.current_iteration += 1

        if task_context.status == "planning" or task_context.status == "replanning":
            agent_type = "planner" if task_context.status == "planning" else "replanner"
            agent = getAgent(agent_type, effort)

            raw_result = agent.invoke({
                "task_context": task_context,
                "input": query,
                "chat_history": history,
            })
            parsed = parse_output(raw_result)

            if parsed.get("status") == "needs_clarification":
                task_context.status = "awaiting_clarification"
                task_context.clarifying_questions = parsed.get("clarifying_questions", [])
                if task_context.clarifying_questions:
                    return jsonify({"response": task_context.clarifying_questions[-1]})
                return jsonify({"response": "Can you please clarify your request?"})
                
            if parsed.get("status") == "ready":
                task_context.status = "executing"
                task_context.plan = parsed.get("plan", {})
                task_context.execution_report = {}
                task_context.verification_report = {}

        elif task_context.status == "executing":
            execution_agent = getAgent("execution", effort)
            raw_result = execution_agent.invoke({
                "task_context": task_context,
                "input": query,
                "chat_history": history,
            })
            parsed = parse_output(raw_result)
            
            task_context.execution_report = parsed.get("execution_report", {})
            if parsed.get("status") == "failed":
                task_context.status = "replanning"
            else:
                task_context.status = "verifying"

        elif task_context.status == "verifying":
            verifier_agent = getAgent("verifier", effort)
            raw_result = verifier_agent.invoke({
                "task_context": task_context,
                "input": query,
                "chat_history": history,
            })
            parsed = parse_output(raw_result)
            
            task_context.verification_report = parsed.get("verification_report", {})
            if parsed.get("status") == "failed":
                task_context.status = "replanning"
            else:
                task_context.status = "completed"


    task_context.status = "completed"
    task_context.plan = {}
    task_context.execution_report = {}
    task_context.verification_report = {}
    task_context.clarifying_questions = []
    task_context.user_notes = []
    task_context.current_iteration = 0

    basic_agent = getAgent("basic", effort)

    result = basic_agent.invoke({
        "task_context": task_context,
        "input": query,
        "system_prompt": "You are given task context. This outlines steps taken by agents to fulfill user query. Your role is to generate a final response to the user. Do not mention any details of agents or the internal workings.",
        "chat_history": history,
    })

    output = result.get("output", "")

    # Update history
    history.append(HumanMessage(content=query))
    history.append(AIMessage(content=output))

    return jsonify({"response": output})
