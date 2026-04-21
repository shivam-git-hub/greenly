from tools.langchain_tools import SearchBudget
budget = SearchBudget(limit=2)
print("1", budget.consume())
print("2", budget.consume())
print("3", budget.consume())
