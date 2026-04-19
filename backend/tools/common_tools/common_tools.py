import io
import json
import logging
import math
import os
import re as _re
import statistics
import threading
import traceback
import datetime as _datetime
from typing import List, Optional

import pandas as pd
import requests
from dotenv import load_dotenv
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .cell_content.write_values import write_values
from .read_structure.read_range import read_range
from .read_structure.read_sheet_structure import read_sheet_structure

load_dotenv()
logger = logging.getLogger(__name__)

# Hard limits
_MAX_QUERY_LEN = 1024
_MAX_SANDBOX_ROWS = 10_000


# ── Web Search ─────────────────────────────────────────────────────────────────

def _is_rate_limited(status_code: int) -> bool:
    """Treat 429 and 5xx as retryable; 4xx auth/validation errors fall through."""
    return status_code == 429 or 500 <= status_code < 600


def web_search(query: str, num_results: int = 5) -> dict:
    """
    Search the web for the given query. Tries DuckDuckGo, then Serper
    (SERPER_API_KEY), then Brave (BRAVE_API_KEY). Falls through to the next
    provider only on rate limits / 5xx / transport errors — not on 4xx auth.
    """
    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": "Query must be a non-empty string"}
    if len(query) > _MAX_QUERY_LEN:
        return {"success": False, "error": f"Query exceeds max length of {_MAX_QUERY_LEN} chars"}

    # 1. DuckDuckGo — free, no key needed
    try:
        from duckduckgo_search import DDGS
        from duckduckgo_search.exceptions import RatelimitException  # type: ignore
    except ImportError:
        DDGS = None
        RatelimitException = Exception  # fallback: any exception triggers fallthrough

    if DDGS is not None:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=num_results))
            if results:
                logger.info("web_search: DuckDuckGo returned %d results", len(results))
                return {"success": True, "source": "duckduckgo", "results": results}
        except RatelimitException as e:
            logger.warning("web_search: DuckDuckGo rate-limited (%s), trying Serper", e)
        except Exception as e:
            logger.warning("web_search: DuckDuckGo failed (%s), trying Serper", e)

    # 2. Serper (Google Search API)
    serper_key = os.getenv("SERPER_API_KEY")
    if serper_key:
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                json={"q": query, "num": num_results},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                results = [
                    {
                        "title": r.get("title"),
                        "body": r.get("snippet"),
                        "href": r.get("link"),
                    }
                    for r in data.get("organic", [])[:num_results]
                ]
                logger.info("web_search: Serper returned %d results", len(results))
                return {"success": True, "source": "serper", "results": results}
            if not _is_rate_limited(resp.status_code):
                # Non-retryable (401/403/400) — surface immediately
                logger.warning("web_search: Serper returned non-retryable HTTP %d", resp.status_code)
                return {
                    "success": False,
                    "source": "serper",
                    "error": f"Serper HTTP {resp.status_code}: {resp.text[:200]}",
                }
            logger.warning("web_search: Serper rate-limited (HTTP %d), trying Brave", resp.status_code)
        except requests.RequestException as e:
            logger.warning("web_search: Serper transport error (%s), trying Brave", e)

    # 3. Brave Search
    brave_key = os.getenv("BRAVE_API_KEY")
    if brave_key:
        try:
            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": brave_key,
                },
                params={"q": query, "count": num_results},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                results = [
                    {
                        "title": r.get("title"),
                        "body": r.get("description"),
                        "href": r.get("url"),
                    }
                    for r in data.get("web", {}).get("results", [])[:num_results]
                ]
                logger.info("web_search: Brave returned %d results", len(results))
                return {"success": True, "source": "brave", "results": results}
            if not _is_rate_limited(resp.status_code):
                logger.warning("web_search: Brave returned non-retryable HTTP %d", resp.status_code)
                return {
                    "success": False,
                    "source": "brave",
                    "error": f"Brave HTTP {resp.status_code}: {resp.text[:200]}",
                }
            logger.warning("web_search: Brave rate-limited (HTTP %d)", resp.status_code)
        except requests.RequestException as e:
            logger.warning("web_search: Brave transport error (%s)", e)

    return {
        "success": False,
        "error": "All search providers failed or are rate-limited. Set SERPER_API_KEY or BRAVE_API_KEY in .env.",
    }


# ── Python Sandbox ─────────────────────────────────────────────────────────────

def _make_safe_print(buf: io.StringIO):
    """Return a print() replacement that writes to `buf` instead of sys.stdout."""
    def _print(*args, sep=" ", end="\n", file=None, flush=False):  # noqa: ARG001
        buf.write(sep.join(str(a) for a in args) + end)
    return _print


def _make_sandbox_namespace() -> dict:
    """
    Return a fresh sandbox namespace with a restricted set of builtins and a
    whitelist of pre-imported modules. `__import__` is intentionally omitted —
    sandbox code cannot import anything beyond what's injected here.
    """
    import builtins

    _SAFE_BUILTINS = (
        "abs", "all", "any", "bin", "bool", "bytes", "callable", "chr",
        "complex", "dict", "dir", "divmod", "enumerate", "filter", "float",
        "format", "frozenset", "getattr", "hasattr", "hash", "hex", "int",
        "isinstance", "issubclass", "iter", "len", "list", "map", "max",
        "min", "next", "object", "oct", "ord", "pow", "print", "range",
        "repr", "reversed", "round", "set", "setattr", "slice", "sorted",
        "str", "sum", "tuple", "type", "zip",
        "__build_class__", "__name__",
    )
    safe_builtins = {k: getattr(builtins, k) for k in _SAFE_BUILTINS if hasattr(builtins, k)}

    ns: dict = {
        "__builtins__": safe_builtins,
        "pd": pd,
        "json": json,
        "math": math,
        "statistics": statistics,
        "datetime": _datetime,
        "re": _re,
    }
    try:
        import numpy as np  # type: ignore
        ns["np"] = np
    except ImportError:
        pass
    return ns


def run_python(code: str, timeout: int = 30, _ns: dict = None) -> dict:
    """
    Execute Python code in a sandboxed namespace. Pre-injected modules:
    pd, np (if installed), math, statistics, datetime, re, json. `import`
    is disabled — code can only use these plus any DataFrames already in _ns.

    stdout is captured per-call by overriding `print` in the namespace's
    builtins (no global sys.stdout mutation), so concurrent runs from
    different sessions don't pollute each other's output.

    Caveat: Python threads cannot be forcibly killed. If execution times
    out, the worker thread may still be running and can mutate _ns after
    this function returns. Avoid long-running operations inside the sandbox.
    """
    ns = _ns if _ns is not None else _make_sandbox_namespace()
    stdout_buf = io.StringIO()

    # Swap in a per-call print that writes to our buffer. This mutates the
    # shared ns["__builtins__"], so within the same session serial execution
    # is assumed (the agent framework runs one tool at a time).
    builtins_dict = ns["__builtins__"]
    original_print = builtins_dict.get("print")
    builtins_dict["print"] = _make_safe_print(stdout_buf)

    state = {"error": None}

    def _target():
        try:
            exec(compile(code, "<sandbox>", "exec"), ns)  # noqa: S102
        except Exception:
            state["error"] = traceback.format_exc()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout)

    # Restore print even if the thread is still alive — any lingering print()
    # calls from a timed-out thread will raise KeyError on builtins access
    # (acceptable since the run already failed).
    if original_print is not None:
        builtins_dict["print"] = original_print

    output = stdout_buf.getvalue()
    if thread.is_alive():
        logger.warning("run_python: timeout after %ds; thread still active, _ns may be corrupted", timeout)
        return {
            "success": False,
            "error": f"Execution timed out after {timeout}s. Sandbox state may be inconsistent — reload any DataFrames before continuing.",
            "output": output,
        }
    if state["error"]:
        return {"success": False, "error": state["error"], "output": output}
    return {"success": True, "output": output}


# ── Sheet → DataFrame ──────────────────────────────────────────────────────────

def _dedupe_headers(raw_headers: list) -> list:
    """Turn a list of header values into unique non-empty column names."""
    seen: dict = {}
    out = []
    for i, h in enumerate(raw_headers):
        name = str(h).strip() if h not in (None, "") else f"Col{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def load_sheet_to_df(
    service,
    spreadsheetId: str,
    sheetTitle: str,
    columns: Optional[List[str]] = None,
    varName: str = "df",
    _ns: dict = None,
) -> dict:
    """
    Read a Google Sheet tab into a pandas DataFrame and store it in the sandbox
    namespace under varName. The first row is treated as column headers.
    Duplicate / empty headers are de-duplicated (e.g. 'Sales', 'Sales_1').
    Rows wider than the header row are preserved by synthesising extra
    'ColN' headers; a warning is returned if this happens.
    """
    warnings: list = []
    try:
        structure = read_sheet_structure(service, spreadsheetId)
        if not structure.get("success"):
            return structure

        sheet_info = next(
            (s for s in structure.get("sheets", []) if s["title"] == sheetTitle),
            None,
        )
        if not sheet_info:
            return {"success": False, "error": f"Sheet '{sheetTitle}' not found in spreadsheet"}

        used_range = sheet_info.get("usedRange", "A1")
        range_str = f"'{sheetTitle}'!{used_range}"

        result = read_range(service, spreadsheetId, range_str)
        if not result.get("success"):
            return result

        cells = result.get("cells", [])
        if not cells:
            return {"success": False, "error": f"Sheet '{sheetTitle}' appears to be empty"}

        raw_headers = [
            (cell.get("value") or cell.get("displayValue"))
            for cell in cells[0]
        ]
        rows = [
            [
                cell.get("value") if cell.get("value") is not None else cell.get("displayValue")
                for cell in row
            ]
            for row in cells[1:]
        ]

        max_row_width = max((len(r) for r in rows), default=0)
        header_width = len(raw_headers)
        if max_row_width > header_width:
            extra = max_row_width - header_width
            raw_headers = list(raw_headers) + [None] * extra
            warnings.append(
                f"{extra} row(s) had more columns than headers; synthesised 'Col{header_width}'.."
                f"'Col{max_row_width - 1}' names for the overflow."
            )

        headers = _dedupe_headers(raw_headers)
        n_cols = len(headers)
        rows = [r + [None] * (n_cols - len(r)) for r in rows]

        df = pd.DataFrame(rows, columns=headers)

        if columns:
            missing = [c for c in columns if c not in df.columns]
            if missing:
                return {"success": False, "error": f"Columns not found in sheet: {missing}"}
            df = df[columns]

        if _ns is not None:
            _ns[varName] = df

        logger.info(
            "load_sheet_to_df: loaded '%s' → '%s' shape=%s",
            sheetTitle, varName, df.shape,
        )
        response = {
            "success": True,
            "varName": varName,
            "shape": list(df.shape),
            "columns": list(df.columns),
            "preview": df.head(5).to_string(index=False),
        }
        if warnings:
            response["warnings"] = warnings
        return response
    except Exception as e:
        logger.error("load_sheet_to_df error: %s", e)
        return {"success": False, "error": str(e)}


# ── DataFrame → Sheet ──────────────────────────────────────────────────────────

def _safe_cell(v):
    """Convert a DataFrame cell to something the Sheets API can accept."""
    if isinstance(v, (list, dict, tuple, set)):
        try:
            return json.dumps(v, default=str)
        except Exception:
            return str(v)
    try:
        return None if pd.isna(v) else v
    except (TypeError, ValueError):
        # pd.isna raises on some array-like objects — fall back to str()
        return str(v)


def write_df_to_sheet(
    service,
    spreadsheetId: str,
    range: str,
    varName: str = "df",
    includeHeader: bool = True,
    _ns: dict = None,
) -> dict:
    """
    Write a pandas DataFrame from the sandbox namespace into a Google Sheet.
    Complex cell values (lists, dicts) are JSON-serialised. NaN becomes None.
    """
    try:
        if _ns is None or varName not in _ns:
            return {
                "success": False,
                "error": (
                    f"Variable '{varName}' not found in the sandbox. "
                    "Load it with load_sheet_to_df or create it with run_python first."
                ),
            }

        obj = _ns[varName]
        if not isinstance(obj, pd.DataFrame):
            return {"success": False, "error": f"'{varName}' is a {type(obj).__name__}, not a DataFrame"}

        df = obj
        if len(df) > _MAX_SANDBOX_ROWS:
            return {
                "success": False,
                "error": f"DataFrame has {len(df)} rows, exceeds limit of {_MAX_SANDBOX_ROWS}",
            }

        rows: List[list] = []
        if includeHeader:
            rows.append([str(c) for c in df.columns])

        for _, row in df.iterrows():
            rows.append([_safe_cell(v) for v in row])

        result = write_values(service, spreadsheetId, range, rows)
        return {
            **result,
            "varName": varName,
            "rowsWritten": len(rows),
            "shape": list(df.shape),
        }
    except Exception as e:
        logger.error("write_df_to_sheet error: %s", e)
        return {"success": False, "error": str(e)}


# ── LangChain tool factories ───────────────────────────────────────────────────

class WebSearchInput(BaseModel):
    query: str = Field(..., description="Search query string")
    num_results: int = Field(5, description="Maximum number of results to return")


class RunPythonInput(BaseModel):
    code: str = Field(..., description="Python code to execute in the sandbox")
    timeout: int = Field(30, description="Maximum execution time in seconds")


class LoadSheetToDfInput(BaseModel):
    spreadsheetId: str = Field(..., description="Google Spreadsheet ID")
    sheetTitle: str = Field(..., description="Tab (sheet) name to load")
    columns: Optional[List[str]] = Field(None, description="Column names to keep. Omit to load all columns.")
    varName: str = Field("df", description="Variable name for the DataFrame in the sandbox")


class WriteDfToSheetInput(BaseModel):
    spreadsheetId: str = Field(..., description="Google Spreadsheet ID")
    range: str = Field(..., description="A1-notation start cell, e.g. 'Sheet1!A1'")
    varName: str = Field("df", description="Sandbox variable name of the DataFrame to write")
    includeHeader: bool = Field(True, description="Write column names as the first row")


def create_python_tools(service) -> list:
    """Python sandbox + Sheet ↔ DataFrame I/O tools sharing one sandbox namespace."""
    ns = _make_sandbox_namespace()

    def _run_python(code: str, timeout: int = 30):
        return json.dumps(run_python(code, timeout, _ns=ns), default=str)

    def _load_sheet_to_df(
        spreadsheetId: str,
        sheetTitle: str,
        columns: Optional[List[str]] = None,
        varName: str = "df",
    ):
        return json.dumps(
            load_sheet_to_df(service, spreadsheetId, sheetTitle, columns, varName, _ns=ns),
            default=str,
        )

    def _write_df_to_sheet(
        spreadsheetId: str,
        range: str,
        varName: str = "df",
        includeHeader: bool = True,
    ):
        return json.dumps(
            write_df_to_sheet(service, spreadsheetId, range, varName, includeHeader, _ns=ns),
            default=str,
        )

    return [
        StructuredTool.from_function(
            func=_run_python,
            name=run_python.__name__,
            description=run_python.__doc__ or run_python.__name__,
            args_schema=RunPythonInput,
        ),
        StructuredTool.from_function(
            func=_load_sheet_to_df,
            name=load_sheet_to_df.__name__,
            description=load_sheet_to_df.__doc__ or load_sheet_to_df.__name__,
            args_schema=LoadSheetToDfInput,
        ),
        StructuredTool.from_function(
            func=_write_df_to_sheet,
            name=write_df_to_sheet.__name__,
            description=write_df_to_sheet.__doc__ or write_df_to_sheet.__name__,
            args_schema=WriteDfToSheetInput,
        ),
    ]


def create_research_tools() -> list:
    """Web search tool. No Sheets service required."""

    def _web_search(query: str, num_results: int = 5):
        return json.dumps(web_search(query, num_results), default=str)

    return [
        StructuredTool.from_function(
            func=_web_search,
            name=web_search.__name__,
            description=web_search.__doc__ or web_search.__name__,
            args_schema=WebSearchInput,
        ),
    ]
