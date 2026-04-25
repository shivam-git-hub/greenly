import json
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
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .llmFactory import get_llm
from tools.langchain_tools import (
    SearchBudget,
    create_read_tools,
    create_research_tools,
    create_skill_tools,
    create_write_tools,
    create_python_tools,
)
from tools.utils import build_sheets_service
from utils import load_access_token_from_file

logger = logging.getLogger(__name__)

# Retry config for per-LLM-call backoff (scratchpad is preserved between retries)
_LLM_RETRY_MAX    = int(os.getenv("LLM_RETRY_MAX", "6"))
_LLM_RETRY_BASE_S = int(os.getenv("LLM_RETRY_BASE_S", "5"))   # doubles each attempt, capped at 120s; overridden by retry-after header when present

# Scratchpad compaction settings
_SCRATCHPAD_FULL_STEPS        = 4       # keep the last N steps at full detail
_SCRATCHPAD_CHAR_BUDGET       = 20_000  # only compact when total output exceeds this
_SCRATCHPAD_OLD_READ_CHARS    = 600     # budget for old read-tool outputs
_SCRATCHPAD_OLD_WRITE_CHARS   = 80      # budget for old write-tool outputs (usually {"success": true})
_SCRATCHPAD_OLD_DEFAULT_CHARS = 500     # budget for everything else

# Tool sets for per-type truncation budgets.
# Research and Python tools are kept full — their outputs are already compact
# or contain structure that breaks when cut mid-way.
_TOOLS_KEEP_FULL = frozenset({
    "web_search", "web_search_financial", "fetch_url",
    "fetch_filing_us", "fetch_filing_india", "load_skill",
    "run_python", "load_sheet_to_df", "write_df_to_sheet",
})
_TOOLS_READ = frozenset({
    "read_sheet_structure", "read_range", "get_chunk",
    "audit_formulas", "trace_dependents",
})
_TOOLS_WRITE = frozenset({
    "write_values", "apply_cell_format", "add_conditional_format",
    "create_chart", "create_pivot_table", "set_data_validation",
    "create_named_range", "create_sheet",
})

# Search budget for the researcher sub-agent (called via the `research` tool).
# This is the effective limit — the search_limit column in getAgent is irrelevant
# for agents that use researcher_tool instead of raw search tools.
_RESEARCHER_SEARCH_LIMIT = int(os.getenv("RESEARCHER_SEARCH_LIMIT", "20"))

# Prompt caching is an Anthropic-only feature. langchain-anthropic forwards
# `cache_control` blocks in message content through to the Messages API.
# Gemini has no equivalent and doesn't take kindly to multi-block system
# messages, so we also skip the two-block system layout when not on Anthropic.
_CACHING_ENABLED = os.getenv("LLM_PROVIDER", "google").lower() == "anthropic"


def _is_rate_limit(exc: Exception) -> bool:
    s = str(exc).lower()
    return "rate_limit" in s or "rate limit" in s or "429" in s or "too many requests" in s


def _get_retry_after(exc: Exception) -> int | None:
    """Read the retry-after header from a rate-limit exception, if the provider sent one."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", {}) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw:
        try:
            return max(1, int(float(raw)))
        except (ValueError, TypeError):
            pass
    return None


def _make_retrying_llm(bound_llm):
    """
    Wrap a bound LLM runnable so that rate-limit errors on individual LLM calls
    are retried. Uses the provider's retry-after header when present; falls back
    to exponential backoff otherwise. The AgentExecutor's scratchpad is NOT part
    of this call — it lives in the AgentExecutor loop — so retrying here
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
                server_wait = _get_retry_after(e)
                wait = server_wait or delay
                logger.warning(
                    "LLM rate limit (attempt %d/%d). Waiting %ds%s. Error: %s",
                    attempt, _LLM_RETRY_MAX, wait,
                    " [retry-after header]" if server_wait else " [backoff]",
                    e,
                )
                time.sleep(wait)
                delay = min(delay * 2, 120)  # always advance so repeated 429s back off further

    return RunnableLambda(_invoke)


def _tool_char_budget(tool_name: str) -> int | None:
    """Return the char budget for an old step's output, or None to keep full."""
    if tool_name in _TOOLS_KEEP_FULL:
        return None
    if tool_name in _TOOLS_READ:
        return _SCRATCHPAD_OLD_READ_CHARS
    if tool_name in _TOOLS_WRITE:
        return _SCRATCHPAD_OLD_WRITE_CHARS
    return _SCRATCHPAD_OLD_DEFAULT_CHARS


def _compact_scratchpad(intermediate_steps: list) -> list:
    """
    Convert intermediate_steps → tool messages with bounded token growth.

    Compaction is skipped entirely when:
    - There are no steps beyond the full window, OR
    - Total output chars are under _SCRATCHPAD_CHAR_BUDGET.

    When triggered, the last _SCRATCHPAD_FULL_STEPS steps are kept at full
    detail. Older steps are truncated per tool type:
    - Research + Python tools: always full (structure breaks when cut)
    - Read tools: up to _SCRATCHPAD_OLD_READ_CHARS
    - Write tools: up to _SCRATCHPAD_OLD_WRITE_CHARS (usually just success flag)
    - Everything else: up to _SCRATCHPAD_OLD_DEFAULT_CHARS
    - Error outputs: always full regardless of tool or age
    """
    if not intermediate_steps:
        return []

    if len(intermediate_steps) <= _SCRATCHPAD_FULL_STEPS:
        return format_to_tool_messages(intermediate_steps)

    total_chars = sum(len(str(output)) for _, output in intermediate_steps)
    if total_chars <= _SCRATCHPAD_CHAR_BUDGET:
        return format_to_tool_messages(intermediate_steps)

    cutoff = max(0, len(intermediate_steps) - _SCRATCHPAD_FULL_STEPS)
    compacted = []
    for i, (action, output) in enumerate(intermediate_steps):
        if i < cutoff:
            out_str = str(output)
            is_error = '"success": false' in out_str or '"error":' in out_str
            budget = None if is_error else _tool_char_budget(action.tool)
            if budget is not None and len(out_str) > budget:
                output = out_str[:budget] + f" … [truncated, {len(out_str)} chars total]"
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

    if first_system_idx == -1:
        logger.info("[cache] No SystemMessage found — breakpoint 1 not placed; caching will miss")
    if last_human_idx == -1:
        logger.info("[cache] No HumanMessage found — breakpoint 2 not placed; caching will miss")

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


def _log_cache_stats(msg):
    """Log Anthropic prompt-cache hit/miss statistics from the LLM response."""
    usage = getattr(msg, "response_metadata", {}).get("usage", {})
    cache_read  = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    uncached    = usage.get("input_tokens", 0)
    output      = usage.get("output_tokens", 0)
    total_input = cache_read + cache_write + uncached

    if total_input:
        hit_pct = round(cache_read / total_input * 100, 1) if total_input else 0
        logger.info(
            "[cache] read=%d write=%d uncached=%d output=%d | hit=%.1f%%",
            cache_read, cache_write, uncached, output, hit_pct,
        )
    return msg


def _build_agent(llm, tools, prompt):
    """
    Build the runnable chain:
      scratchpad compaction → prompt render → (optional) cache_control →
      rate-limit-retrying LLM call → cache stats logging → ToolsAgentOutputParser.
    """
    bound_llm    = llm.bind_tools(tools)
    retrying_llm = _make_retrying_llm(bound_llm)

    chain = RunnablePassthrough.assign(
        agent_scratchpad=lambda x: _compact_scratchpad(x["intermediate_steps"])
    ) | prompt

    if _CACHING_ENABLED:
        chain = chain | RunnableLambda(_apply_anthropic_cache)

    return chain | retrying_llm | RunnableLambda(_log_cache_stats) | ToolsAgentOutputParser()


def _build_skills_catalog() -> str:
    """
    Scan backend/skills/ and build a compact catalog for injection into the
    static system prompt. Each SKILL.md must follow the standard format:

        # <Skill Name>
        > <one-line description>

    Only the H1 title and the first blockquote line are extracted; the rest
    of the file is ignored here (agents call load_skill() for full details).
    Returns an empty string if no skills are found so callers can skip safely.
    """
    skills_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "skills"))
    if not os.path.isdir(skills_dir):
        return ""

    entries = []
    for skill_name in sorted(os.listdir(skills_dir)):
        skill_md = os.path.join(skills_dir, skill_name, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        title = skill_name
        description = ""
        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("# ") and title == skill_name:
                        title = stripped[2:].strip()
                    elif stripped.startswith("> "):
                        description = stripped[2:].strip()
                        break
        except Exception:
            pass
        entries.append(f"- **{skill_name}** — {title}: {description}" if description else f"- **{skill_name}** — {title}")

    if not entries:
        return ""

    return (
        "## Available Skills\n"
        "Call load_skill(skill_name) with the bold key to get full step-by-step instructions:\n\n"
        + "\n".join(entries)
    )


# Computed once at import — scanning skills dir on every LLMAgents construction was wasteful
_SKILLS_CATALOG = _build_skills_catalog()


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


class ResearchAgentInput(BaseModel):
    query: str = Field(
        ...,
        description=(
            "Detailed description of what to research. Include: the company or entity, "
            "the specific data points needed (e.g. revenue, EBITDA margin, WACC), "
            "the time period, and preferred source types (SEC filing, BSE AR, IR page). "
            "The more specific, the better." 
        ),
    )


class LLMAgents:
    def __init__(self, llm_type: str = "fast"):

        self.llm = get_llm(llm_type)

        self.access_token = load_access_token_from_file()
        self.sheet_service = build_sheets_service(self.access_token)

        self.search_budget     = SearchBudget(limit=5)
        self.research_tools    = create_research_tools(budget=self.search_budget)
        # TODO: skill caching optimization
        # Currently loaded skill content (5–50KB markdown) lives in the agent scratchpad,
        # which is never covered by a cache breakpoint — every LLM call after load_skill
        # re-sends it uncached. Two improvements to make when ready:
        #
        # Part A — Cross-phase persistence: add loaded_skills: dict to TaskContext.
        #   The load_skill wrapper writes to it. Each subsequent agent.invoke() pre-populates
        #   a SkillsCache (like SearchBudget) from task_context.loaded_skills so planner →
        #   execution → verifier all share the content without re-loading.
        #
        # Part B — In-invoke caching: add a 3rd system block [static | skills | task_context].
        #   A RunnablePassthrough.assign step reads from a shared SkillsCache closure and
        #   fills the skills block dynamically. _apply_anthropic_cache marks that block so
        #   from the 2nd LLM call after load_skill the skill is cached at the system level
        #   rather than re-sent uncached in the scratchpad on every subsequent tool call.
        self.skill_tools       = create_skill_tools()
        self.read_tools        = create_read_tools(self.sheet_service)
        self.write_tools       = create_write_tools(self.sheet_service)
        self.python_tools      = create_python_tools(self.sheet_service)

        prompts_dir = os.path.join(os.path.dirname(__file__), "prompts")

        def _load(name):
            with open(os.path.join(prompts_dir, name), "r", encoding="utf-8") as f:
                return f.read().strip()

        def _with_catalog(text: str) -> str:
            return text + ("\n\n" + _SKILLS_CATALOG if _SKILLS_CATALOG else "")

        self.plannerPromptTemplate    = self._build_template(_with_catalog(_load("planner.txt")))
        self.replannerPromptTemplate  = self._build_template(_with_catalog(_load("replanner.txt")))
        self.researcherPromptTemplate = self._build_template(_load("researcher.txt"))
        self.verifierPromptTemplate   = self._build_template(_with_catalog(_load("verifier.txt")))
        self.executionPromptTemplate  = self._build_template(_load("execution.txt"))
        self.genericPromptTemplate    = self._build_template(_load("generic.txt"))

        # Researcher sub-agent: a full AgentExecutor wrapped as a single tool so
        # that planner/replanner/execution can delegate all search orchestration
        # to it instead of calling raw search tools directly.
        # Always uses the fast LLM — it runs as a tool call within the outer agent's
        # loop and search quality/cost matters more than reasoning depth here.
        self.researcherExecutor = AgentExecutor(
            agent=_build_agent(get_llm("fast"), self.research_tools, self.researcherPromptTemplate),
            tools=self.research_tools,
            max_iterations=50,
            verbose=True,
            handle_parsing_errors=True,
        )

        def _invoke_researcher(query: str) -> str:
            self.search_budget.reset(limit=_RESEARCHER_SEARCH_LIMIT)
            try:
                result = self.researcherExecutor.invoke({
                    "input": query,
                    "task_context": "",  # researcher operates on the query alone; no pipeline context needed
                })
                return result.get("output", "Research sub-agent returned no output.")
            except Exception as e:
                return f"Research failed: {e}"

        self.researcher_tool = StructuredTool.from_function(
            func=_invoke_researcher,
            name="research",
            description=(
                "Spawn a dedicated research sub-agent that searches the web, fetches filings, "
                "and returns clean structured findings with source citations. "
                "Pass a detailed query describing the entity, specific data points, time period, "
                "and preferred source type. The sub-agent handles all search orchestration — "
                "do NOT call web_search or fetch_url yourself."
            ),
            args_schema=ResearchAgentInput,
            handle_tool_error=True,
        )

        self.planner_tools      = self.read_tools + self.skill_tools + [self.researcher_tool]
        self.replanner_tools    = self.read_tools + self.skill_tools + [self.researcher_tool]
        self.execution_tools    = self.write_tools + self.read_tools + self.python_tools + [self.researcher_tool]
        self.verification_tools = self.read_tools + self.python_tools + self.skill_tools
        self.basic_tools        = []
        self.generic_tools      = self.read_tools + self.write_tools + self.python_tools + self.research_tools

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



# Module-level cache: LLMAgents is expensive to build (LLM client, Sheets service,
# tool factories, prompt file reads, researcher executor). Safe to reuse across
# requests for a single-user add-on — the only mutable state is search_budget,
# which is always reset before each agent call via getAgent / _invoke_researcher.
_llm_agents_cache: dict = {}


def getAgent(agentType: str, llm_type: str = "fast"):
    """Factory: return a configured AgentExecutor for the requested agent type."""
    if llm_type not in _llm_agents_cache:
        _llm_agents_cache[llm_type] = LLMAgents(llm_type)
    llmAgents = _llm_agents_cache[llm_type]

    # (agent, tools, max_iterations, search_budget_limit)
    # search_budget_limit=0 means no search tools — budget reset is a no-op
    agents = {
        "planner":    (llmAgents.plannerAgent,    llmAgents.planner_tools,      50, 0),
        "replanner":  (llmAgents.replannerAgent,  llmAgents.replanner_tools,    50, 0),
        "researcher": (llmAgents.researcherAgent, llmAgents.research_tools,     50, 8),
        "verifier":   (llmAgents.verifierAgent,   llmAgents.verification_tools, 50, 0),
        "execution":  (llmAgents.executionAgent,  llmAgents.execution_tools,    60, 0),
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
