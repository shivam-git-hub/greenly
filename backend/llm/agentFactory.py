import os
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from .llmFactory import get_llm
from tools.langchain_tools import create_read_tools, create_research_tools
from tools.utils import build_sheets_service
from utils import load_access_token_from_file
from tools.langchain_tools import create_read_tools, create_research_tools, create_write_tools, create_python_tools


class LLMAgents:
    def __init__(self, llm_type: str = "fast"):

        self.llm = get_llm(llm_type)

        self.access_token = load_access_token_from_file()
        self.sheet_service = build_sheets_service(self.access_token)

        self.research_tools = create_research_tools()
        self.read_tools = create_read_tools(self.sheet_service)
        self.write_tools = create_write_tools(self.sheet_service)
        self.python_tools = create_python_tools(self.sheet_service)

        self.planner_tools = self.research_tools + self.read_tools
        self.execution_tools = self.write_tools + self.read_tools + self.python_tools + self.research_tools
        self.verification_tools = self.read_tools + self.python_tools
        self.compaction_tools = []
        self.basic_tools = []
        self.generic_tools = self.read_tools + self.write_tools + self.python_tools + self.research_tools

        self.planner_prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "planner.txt")
        self.replanner_prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "replanner.txt")
        self.researcher_prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "researcher.txt")
        self.verifier_prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "verifier.txt")
        self.execution_prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "execution.txt")
        self.generic_prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "generic.txt")

        with open(self.planner_prompt_path, "r", encoding="utf-8") as f:
            self.planner_prompt = f.read().strip()
        with open(self.replanner_prompt_path, "r", encoding="utf-8") as f:
            self.replanner_prompt = f.read().strip()
        with open(self.researcher_prompt_path, "r", encoding="utf-8") as f:
            self.researcher_prompt = f.read().strip()
        with open(self.verifier_prompt_path, "r", encoding="utf-8") as f:
            self.verifier_prompt = f.read().strip()
        with open(self.execution_prompt_path, "r", encoding="utf-8") as f:
            self.execution_prompt = f.read().strip()
        with open(self.generic_prompt_path, "r", encoding="utf-8") as f:
            self.generic_prompt = f.read().strip()

        self.plannerPromptTemplate = ChatPromptTemplate.from_messages([
            MessagePlaceholder(variable_name="task_context", optional=True),
            ("system", self.planner_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        self.replannerPromptTemplate = ChatPromptTemplate.from_messages([
            MessagePlaceholder(variable_name="task_context", optional=True),
            ("system", self.replanner_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        self.researcherPromptTemplate = ChatPromptTemplate.from_messages([
            MessagePlaceholder(variable_name="task_context", optional=True),
            ("system", self.researcher_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        self.verifierPromptTemplate = ChatPromptTemplate.from_messages([
            MessagePlaceholder(variable_name="task_context", optional=True),
            ("system", self.verifier_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        self.executionPromptTemplate = ChatPromptTemplate.from_messages([
            MessagePlaceholder(variable_name="task_context", optional=True),
            ("system", self.execution_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        self.genericPromptTemplate = ChatPromptTemplate.from_messages([
            MessagePlaceholder(variable_name="task_context", optional=True),
            ("system", self.generic_prompt),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        self.basicPromptTemplate = ChatPromptTemplate.from_messages([
            MessagePlaceholder(variable_name="task_context", optional=True),
            ("system", "{system_prompt}"),
            MessagesPlaceholder(variable_name="chat_history", optional=True),
            ("user", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])

        self.plannerAgent = create_tool_calling_agent(self.llm, self.planner_tools, self.plannerPromptTemplate)
        self.replannerAgent = create_tool_calling_agent(self.llm, self.replanner_tools, self.replannerPromptTemplate)
        self.researcherAgent = create_tool_calling_agent(self.llm, self.research_tools, self.researcherPromptTemplate)
        self.verifierAgent = create_tool_calling_agent(self.llm, self.verification_tools, self.verifierPromptTemplate)
        self.executionAgent = create_tool_calling_agent(self.llm, self.execution_tools, self.executionPromptTemplate)
        self.genericAgent = create_tool_calling_agent(self.llm, self.generic_tools, self.genericPromptTemplate)
        self.basicAgent = create_tool_calling_agent(self.llm, self.basic_tools, self.basicPromptTemplate)

pro_llmAgents = LLMAgents("pro")
fast_llmAgents = LLMAgents("fast")

def getAgent(agentType: str, llm_type: str = "fast"):
    """
    Factory function to create and return Langchain tool-calling agents.
    
    Args:
        agentType (str): The type of agent to create (e.g., "plannerAgent").
        access_token (str, optional): The OAuth token for Google Sheets API.
                                      Required to initialize read_tools.
                                      
    Returns:
        AgentExecutor: The initialized Langchain agent executor.
    """

    llmAgents = None

    if llm_type == "pro":
        llmAgents = pro_llmAgents
    else:
        llmAgents = fast_llmAgents

    if agentType == "planner":
        return AgentExecutor(agent=llmAgents.plannerAgent, tools=llmAgents.planner_tools,max_iterations=30, verbose=True)
    if agentType == "replanner":
        return AgentExecutor(agent=llmAgents.replannerAgent, tools=llmAgents.replanner_tools,max_iterations=30, verbose=True)
    if agentType == "researcher":
        return AgentExecutor(agent=llmAgents.researcherAgent, tools=llmAgents.research_tools,max_iterations=30, verbose=True)
    if agentType == "verifier":
        return AgentExecutor(agent=llmAgents.verifierAgent, tools=llmAgents.verification_tools,max_iterations=30, verbose=True)
    if agentType == "execution":
        return AgentExecutor(agent=llmAgents.executionAgent, tools=llmAgents.execution_tools,max_iterations=30, verbose=True)
    if agentType == "generic":
        return AgentExecutor(agent=llmAgents.genericAgent, tools=llmAgents.generic_tools,max_iterations=5, verbose=True)
    if agentType == "basic":
        return AgentExecutor(agent=llmAgents.basicAgent, tools=llmAgents.basic_tools,max_iterations=5, verbose=True)

    raise ValueError(f"Unknown agentType: {agentType}")
