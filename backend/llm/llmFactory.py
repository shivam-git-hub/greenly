from langchain_google_genai import ChatGoogleGenerativeAI

def get_llm(type: str = "fast"):
    """
    Creates and returns a LangChain LLM object based on the specified type.
    
    Args:
        type (str): If "fast", returns a Gemini 3 Flash based LLM.
                    Otherwise, returns a Gemini 3 Pro based LLM.
                    
    Returns:
        ChatGoogleGenerativeAI: The LangChain agent executor compatible LLM object.
    """
    if type == "fast":
        return ChatGoogleGenerativeAI(model="gemini-2.5-flash")
    else:
        return ChatGoogleGenerativeAI(model="gemini-2.5-pro")
