
import os
from dotenv import load_dotenv

load_dotenv()

_PROVIDER = os.getenv("LLM_PROVIDER", "google").lower()

# Anthropic models
_ANTHROPIC_FAST = "claude-haiku-4-5-20251001"
_ANTHROPIC_PRO  = "claude-sonnet-4-6"

# Google models
_GOOGLE_FAST = "gemini-2.5-flash"
_GOOGLE_PRO  = "gemini-2.5-pro"


def get_llm(type: str = "fast"):
    """
    Returns a LangChain-compatible LLM based on LLM_PROVIDER in .env.

    Provider "google"    — fast: Gemini 2.5 Flash, pro: Gemini 2.5 Pro
    Provider "anthropic" — fast: Claude Haiku 4.5,  pro: Claude Sonnet 4.6
    """
    if _PROVIDER == "anthropic":
        from langchain_anthropic import ChatAnthropic
        model = _ANTHROPIC_FAST if type == "fast" else _ANTHROPIC_PRO
        return ChatAnthropic(model=model, max_tokens=8192)
    else:
        from langchain_google_genai import ChatGoogleGenerativeAI
        model = _GOOGLE_FAST if type == "fast" else _GOOGLE_PRO
        return ChatGoogleGenerativeAI(model=model, max_tokens=8192)
