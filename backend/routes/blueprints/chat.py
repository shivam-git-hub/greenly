from flask import Blueprint, request, jsonify
from langchain_core.messages import HumanMessage, AIMessage

from backend.llm.agentFactory import getAgent

chat_bp = Blueprint("chat", __name__)

# In-memory chat history keyed by spreadsheetId
_chat_histories: dict[str, list] = {}


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

    # Build a context-enriched input for the agent
    context_prefix = (
        f"Spreadsheet ID: {spreadsheet_id}\n"
        f"Active sheet: {active_sheet} (sheetId={active_sheet_id})\n"
        f"Active range: {active_range}\n"
    )
    agent_input = context_prefix + query

    # Retrieve or initialise per-spreadsheet chat history
    history = _chat_histories.setdefault(spreadsheet_id, [])

    agent_type = "planner"
    executor = getAgent(agent_type, effort)

    result = executor.invoke({
        "input": agent_input,
        "chat_history": history,
    })

    output = result.get("output", "")

    # Update history
    history.append(HumanMessage(content=agent_input))
    history.append(AIMessage(content=output))

    return jsonify({"response": output})
