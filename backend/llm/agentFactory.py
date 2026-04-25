import logging
import os
import time

from langchain.agents import AgentExecutor
from langchain.agents.format_scratchpad.tools import format_to_tool_messages
from langchain.agents.output_parsers.tools import ToolsAgentOutputParser
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompt_values import ChatPromptValue
from langchain_core.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
)
from langchain_core.runnables import RunnableLambda, RunnablePassthrough

from .llmFactory import get_llm
from tools.langchain_tools import (
    SearchBudget,
    create_read_tools,
    create_research_tools,
    create_write_tools,
    create_python_tools,
)
from tools.utils import build_sheets_service
from utils import load_access_token_from_file

logger = logging.getLogger(__name__)

# Retry config for per-LLM-call backoff (scratchpad is preserved between retries)
_LLM_RETRY_MAX    = 6
_LLM_RETRY_BASE_S = 15   # doubles each attempt, capped at 120s

# Scratchpad compaction settings
_SCRATCHPAD_FULL_STEPS    = 4    # keep the last N steps at full detail
_SCRATCHPAD_OLD_OUT_CHARS = 250  # max chars kept for older tool outputs

# Prompt caching is an Anthropic-only feature. langchain-anthropic forwards
# `cache_control` blocks in message content through to the Messages API.
# Gemini has no equivalent and doesn't take kindly to multi-block system
# messages, so we also skip the two-block system layout when not on Anthropic.
_CACHING_ENABLED = os.getenv("LLM_PROVIDER", "google").lower() == "anthropic"


def _is_rate_limit(exc: Exception) -> bool:
    s = str(exc).lower()
    return "rate_limit" in s or "rate limit" in s or "429" in s or "too many requests" in s


def _make_retrying_llm(bound_llm):
    """
    Wrap a bound LLM runnable so that rate-limit errors on individual LLM calls
    are retried with exponential backoff. The AgentExecutor's scratchpad is NOT
    part of this call — it lives in the AgentExecutor loop — so retrying here
    preserves all accumulated research context.
    """
    def _invoke(input, config=None):
        delay = _LLM_RETRY_BASE_S
        for attempt in range(1, _LLM_RETRY_MAX + 1):
            try:
                return bound_llm.invoke(input, config=config)
            except Exception as e:
                if not _is_rate_limit(e) or attempt == _LLM_RETRY_MAX:
                    raise
                logger.warning(
                    "LLM rate limit (attempt %d/%d). Backing off %ds. Error: %s",
                    attempt, _LLM_RETRY_MAX, delay, e,
                )
                time.sleep(delay)
                delay = min(delay * 2, 120)

    return RunnableLambda(_invoke)


def _compact_scratchpad(intermediate_steps: list) -> list:
    """
    Convert intermediate_steps → tool messages with bounded token growth.

    The last _SCRATCHPAD_FULL_STEPS steps are kept at full detail (the model
    needs recent context). Older steps have their tool output truncated to
    _SCRATCHPAD_OLD_OUT_CHARS chars — the model can still see it called the
    tool and what it roughly got, without re-reading the full response.
    """
    if not intermediate_steps:
        return []

    cutoff = max(0, len(intermediate_steps) - _SCRATCHPAD_FULL_STEPS)
    compacted = []
    for i, (action, output) in enumerate(intermediate_steps):
        if i < cutoff:
            out_str = str(output)
            if len(out_str) > _SCRATCHPAD_OLD_OUT_CHARS:
                output = (
                    out_str[:_SCRATCHPAD_OLD_OUT_CHARS]
                    + f" … [truncated, {len(out_str)} chars total]"
                )
        compacted.append((action, output))

    return format_to_tool_messages(compacted)


def _mark_block(content, position: int):
    """
    Return a copy of `content` with `cache_control: ephemeral` added to the
    block at `position` (negatives indexed from the end). Strings are promoted
    to a single-block list. Non-dict blocks are skipped.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]

    if not isinstance(content, list) or not content:
        return content

    idx = position if position >= 0 else len(content) + position
    new_content = []
    for i, block in enumerate(content):
        if i == idx and isinstance(block, dict):
            new_content.append({**block, "cache_control": {"type": "ephemeral"}})
        else:
            new_content.append(block)
    return new_content


def _apply_anthropic_cache(prompt_value):
    """
    Insert Anthropic prompt-caching breakpoints into the rendered prompt.

    Caching is a prefix match: tools and `system` render first in every
    Messages API request, so any byte change anywhere in the prefix
    invalidates everything after it. We lay down two breakpoints:

      1. End of the static system block. The system message is built with
         two content blocks — [static_prompt, task_context] — and we mark
         block 0 only. The cached prefix is [tools + static_prompt], so
         this breakpoint survives across every invocation of the same
         agent regardless of task_context / history / input.

      2. End of the last user message. Within a single agent.invoke() the
         tools, system, chat_history, and user input are all frozen; only
         the scratchpad grows. This breakpoint caches the entire prefix up
         to the user input, so every LLM call after the first in a single
         AgentExecutor loop reads ~everything from cache.

    Max 4 cache_control blocks per request (we use 2). When a breakpoint's
    prefix changes, that breakpoint's cache misses but earlier breakpoints
    still read — so breakpoint 1 keeps paying off even when breakpoint 2
    invalidates between iterations.
    """
    if isinstance(prompt_value, ChatPromptValue):
        messages = prompt_value.to_messages()
    else:
        messages = list(prompt_value)

    first_system_idx = next(
        (i for i, m in enumerate(messages) if isinstance(m, SystemMessage)),
        -1,
    )
    last_human_idx = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
        -1,
    )

    new_messages = []
    for i, msg in enumerate(messages):
        if i == first_system_idx:
            # Block 0 (static) is the cache target. Block 1 (task_context) is
            # volatile and must NOT get a cache_control marker.
            msg = SystemMessage(content=_mark_block(msg.content, position=0))
        elif i == last_human_idx:
            # Single-block user input; mark the last (only) block.
            msg = HumanMessage(content=_mark_block(msg.content, position=-1))
        new_messages.append(msg)

    return new_messages


def _build_agent(llm, tools, prompt):
    """
    Build the runnable chain:
      scratchpad compaction → prompt render → (optional) cache_control →
      rate-limit-retrying LLM call → ToolsAgentOutputParser.
    """
    bound_llm    = llm.bind_tools(tools)
    retrying_llm = _make_retrying_llm(bound_llm)

    chain = RunnablePassthrough.assign(
        agent_scratchpad=lambda x: _compact_scratchpad(x["intermediate_steps"])
    ) | prompt

    if _CACHING_ENABLED:
        chain = chain | RunnableLambda(_apply_anthropic_cache)

    return chain | retrying_llm | ToolsAgentOutputParser()


def _system_template(static_text: str):
    """
    Build the system message template. On Anthropic we split into two content
    blocks so the static half is byte-identical across calls and safe to
    cache; on other providers we keep the original single-string form.

    `static_text` is expected to already be LangChain-escaped (literal braces
    doubled) — the volatile `{task_context}` variable lives only in the
    second block, which we own here.
    """
    if _CACHING_ENABLED:
        return SystemMessagePromptTemplate.from_template([
            {"type": "text", "text": static_text},
            {"type": "text", "text": "\n\n## Current Task Context\n{task_context}"},
        ])
    return ("system", static_text + "\n\n## Current Task Context\n{task_context}")


class LLMAgents:
    def __init__(self, llm_type: str = "fast"):

        self.llm = get_llm(llm_type)

        self.access_token = load_access_token_from_file()
        self.sheet_service = build_sheets_service(self.access_token)

        self.search_budget     = SearchBudget(limit=5)
        self.research_tools    = create_research_tools(budget=self.search_budget)
        self.read_tools        = create_read_tools(self.sheet_service)
        self.write_tools       = create_write_tools(self.sheet_service)
        self.python_tools      = create_python_tools(self.sheet_service)

        self.planner_tools     = self.research_tools + self.read_tools
        self.replanner_tools   = self.research_tools + self.read_tools
        self.execution_tools   = self.write_tools + self.read_tools + self.python_tools + self.research_tools
        self.verification_tools = self.read_tools + self.python_tools
        self.basic_tools       = []
        self.generic_tools     = self.read_tools + self.write_tools + self.python_tools + self.research_tools

        prompts_dir = os.path.join(os.path.dirname(__file__), "prompts")

        def _load(name):
            with open(os.path.join(prompts_dir, name), "r", encoding="utf-8") as f:
                return f.read().strip()

        self.plannerPromptTemplate    = self._build_template(_load("planner.txt"))
        self.replannerPromptTemplate  = self._build_template(_load("replanner.txt"))
        self.researcherPromptTemplate = self._build_template(_load("researcher.txt"))
        self.verifierPromptTemplate   = self._build_template(_load("verifier.txt"))
        self.executionPromptTemplate  = self._build_template(_load("execution.txt"))
        self.genericPromptTemplate    = self._build_template(_load("generic.txt"))

        # Basic agent's system prompt is injected at runtime via {system_prompt}
        # (three possible short strings from chat.py). It's below the
        # min-cacheable threshold so caching the system block is a no-op in
        # practice, but the structure is kept consistent so _apply_anthropic_cache
        # doesn't need a special case.
        self.basicPromptTemplate = ChatPromptTemplate.from_messages([
            _system_template("{system_prompt}"),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        self.plannerAgent    = _build_agent(self.llm, self.planner_tools,      self.plannerPromptTemplate)
        self.replannerAgent  = _build_agent(self.llm, self.replanner_tools,    self.replannerPromptTemplate)
        self.researcherAgent = _build_agent(self.llm, self.research_tools,     self.researcherPromptTemplate)
        self.verifierAgent   = _build_agent(self.llm, self.verification_tools, self.verifierPromptTemplate)
        self.executionAgent  = _build_agent(self.llm, self.execution_tools,    self.executionPromptTemplate)
        self.genericAgent    = _build_agent(self.llm, self.generic_tools,      self.genericPromptTemplate)
        self.basicAgent      = _build_agent(self.llm, self.basic_tools,        self.basicPromptTemplate)

    @staticmethod
    def _build_template(system_prompt: str) -> ChatPromptTemplate:
        # Escape literal braces so LangChain doesn't treat JSON examples in the
        # loaded prompt as template variables. The {task_context} variable
        # lives in the volatile second system block and stays unescaped.
        escaped = system_prompt.replace("{", "{{").replace("}", "}}")
        return ChatPromptTemplate.from_messages([
            _system_template(escaped),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])



def getAgent(agentType: str, llm_type: str = "fast"):
    """Factory: return a configured AgentExecutor for the requested agent type."""
    pro_llmAgents  = LLMAgents("pro")
    fast_llmAgents = LLMAgents("fast")

    llmAgents = pro_llmAgents if llm_type == "pro" else fast_llmAgents

    # (agent, tools, max_iterations, search_budget_limit)
    # search_budget_limit=0 means no search tools — budget reset is a no-op
    agents = {
        "planner":    (llmAgents.plannerAgent,    llmAgents.planner_tools,      50, 6),
        "replanner":  (llmAgents.replannerAgent,  llmAgents.replanner_tools,    50, 4),
        "researcher": (llmAgents.researcherAgent, llmAgents.research_tools,     50, 8),
        "verifier":   (llmAgents.verifierAgent,   llmAgents.verification_tools, 50, 0),
        "execution":  (llmAgents.executionAgent,  llmAgents.execution_tools,    60, 3),
        "generic":    (llmAgents.genericAgent,    llmAgents.generic_tools,      50, 5),
        "basic":      (llmAgents.basicAgent,      llmAgents.basic_tools,         5, 0),
    }

    if agentType not in agents:
        raise ValueError(f"Unknown agentType: {agentType}")

    agent, tools, max_iter, search_limit = agents[agentType]
    if search_limit > 0:
        llmAgents.search_budget.reset(limit=search_limit)

    return AgentExecutor(
        agent=agent,
        tools=tools,
        max_iterations=max_iter,
        verbose=True,
        handle_parsing_errors=True,
    )
