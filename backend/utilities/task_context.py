from dataclasses import dataclass, field

@dataclass
class TaskContext:
    user_request: str
    spreadsheet_id: str
    active_sheet_id: str | None = None
    
    sheet_structure: dict | None = None

    # Set by planner
    plan: dict | None = None
    clarifying_questions: list[str] = field(default_factory=list)
    user_notes: list[str] = field(default_factory=list)
    
    # Set by executor
    execution_report: dict | None = None
    
    # Set by verifier
    verification_report: dict | None = None
    
    # Loop control
    current_iteration: int = 0
    max_iteration: int = 50
    status: str = "completed"  # planning | awaiting_clarification | executing | verifying | completed | replanning | failed