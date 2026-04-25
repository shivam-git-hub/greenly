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

try:
    import trafilatura as _trafilatura
    _TRAFILATURA_AVAILABLE = True
except ImportError:
    _trafilatura = None
    _TRAFILATURA_AVAILABLE = False

from tools.cell_content.write_values import write_values
from tools.read_structure.read_range import read_range
from tools.read_structure.read_sheet_structure import read_sheet_structure

load_dotenv()
logger = logging.getLogger(__name__)

# Hard limits
_MAX_QUERY_LEN = 1024
_MAX_SANDBOX_ROWS = 10_000
_SEARCH_BODY_MAX = 300   # chars per search result snippet — enough context, not a full page
_SEARCH_TITLE_MAX = 120  # chars per title


# ── Web Search ─────────────────────────────────────────────────────────────────

def _is_rate_limited(status_code: int) -> bool:
    """Treat 429 and 5xx as retryable; 4xx auth/validation errors fall through."""
    return status_code == 429 or 500 <= status_code < 600


def _trim_result(r: dict) -> dict:
    """Truncate search result fields to stay within token budget."""
    title = (r.get("title") or "")[:_SEARCH_TITLE_MAX]
    body  = (r.get("body") or r.get("snippet") or r.get("description") or "")[:_SEARCH_BODY_MAX]
    href  = r.get("href") or r.get("link") or r.get("url") or ""
    return {"title": title, "body": body, "href": href}


def web_search(query: str, num_results: int = 5) -> dict:
    """
    Search the web for the given query. Tries Brave (BRAVE_API_KEY), then Serper
    (SERPER_API_KEY), then DuckDuckGo. Falls through to the next provider only on
    rate limits / 5xx / transport errors — not on 4xx auth.
    """
    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": "Query must be a non-empty string"}
    if len(query) > _MAX_QUERY_LEN:
        return {"success": False, "error": f"Query exceeds max length of {_MAX_QUERY_LEN} chars"}

    # 1. Brave Search
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
                    _trim_result(r)
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
            logger.warning("web_search: Brave rate-limited (HTTP %d), trying Serper", resp.status_code)
        except requests.RequestException as e:
            logger.warning("web_search: Brave transport error (%s), trying Serper", e)

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
                    _trim_result(r)
                    for r in data.get("organic", [])[:num_results]
                ]
                logger.info("web_search: Serper returned %d results", len(results))
                return {"success": True, "source": "serper", "results": results}
            if not _is_rate_limited(resp.status_code):
                logger.warning("web_search: Serper returned non-retryable HTTP %d", resp.status_code)
                return {
                    "success": False,
                    "source": "serper",
                    "error": f"Serper HTTP {resp.status_code}: {resp.text[:200]}",
                }
            logger.warning("web_search: Serper rate-limited (HTTP %d), trying DuckDuckGo", resp.status_code)
        except requests.RequestException as e:
            logger.warning("web_search: Serper transport error (%s), trying DuckDuckGo", e)

    # 3. DuckDuckGo — free, no key needed
    try:
        from duckduckgo_search import DDGS
        from duckduckgo_search.exceptions import RatelimitException  # type: ignore
    except ImportError:
        DDGS = None
        RatelimitException = Exception

    if DDGS is not None:
        try:
            with DDGS() as ddgs:
                results = [_trim_result(r) for r in ddgs.text(query, max_results=num_results)]
            if results:
                logger.info("web_search: DuckDuckGo returned %d results", len(results))
                return {"success": True, "source": "duckduckgo", "results": results}
        except RatelimitException as e:
            logger.warning("web_search: DuckDuckGo rate-limited (%s)", e)
        except Exception as e:
            logger.warning("web_search: DuckDuckGo failed (%s)", e)

    return {
        "success": False,
        "error": "All search providers failed or are rate-limited. Set BRAVE_API_KEY or SERPER_API_KEY in .env.",
    }


# ── URL Fetcher ────────────────────────────────────────────────────────────────

_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GreenlyCrawler/1.0)"
}
_MAX_FETCH_BYTES = 10 * 1024 * 1024  # 10 MB cap before parsing


def fetch_url(
    url: str,
    max_chars: int = 8000,
    extract_tables: bool = True,
    headers: Optional[dict] = None,
) -> dict:
    """
    Fetch the text content of a URL. Supports HTML web pages and PDF files.
    Use this after web_search to read the actual content of a page or document.

    For PDFs, also extracts structured tables when extract_tables=True (default).
    Returns up to max_chars characters of extracted text. Tables are returned
    as a list of 2-D arrays (rows of cells) under the `tables` key.

    Use the `headers` arg to override the default User-Agent — required for
    sources like SEC EDGAR which block default crawler agents.
    """
    if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")):
        return {"success": False, "url": url, "error": "url must start with http:// or https://"}

    req_headers = {**_FETCH_HEADERS, **(headers or {})}

    try:
        resp = requests.get(url, headers=req_headers, timeout=20, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").lower()
        is_pdf = "application/pdf" in content_type or url.lower().split("?")[0].endswith(".pdf")

        # E4: accumulate chunks into a list, join once — avoids O(n²) bytes reallocation
        raw_parts: list = []
        total_bytes = 0
        for chunk in resp.iter_content(chunk_size=65536):
            raw_parts.append(chunk)
            total_bytes += len(chunk)
            if total_bytes >= _MAX_FETCH_BYTES:
                break
        raw = b"".join(raw_parts)

        tables: list = []
        if is_pdf:
            import pdfplumber
            text_parts = []
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                    if extract_tables:
                        try:
                            page_tables = page.extract_tables() or []
                            for tbl in page_tables:
                                # Skip empty / single-row noise
                                cleaned = [[(c or "").strip() for c in row] for row in tbl if any(row)]
                                if len(cleaned) >= 2:
                                    tables.append(cleaned)
                        except Exception as e:
                            logger.debug("fetch_url: table extract failed on page (%s)", e)
            text = "\n\n".join(text_parts)
            detected_type = "pdf"
        elif "text/html" in content_type:
            # E3: pass raw bytes — trafilatura and BeautifulSoup both detect encoding
            # from the byte stream / <meta charset>, avoiding UTF-8 decode corruption
            text = ""
            if _TRAFILATURA_AVAILABLE:
                try:
                    text = _trafilatura.extract(
                        raw,
                        include_tables=False,
                        include_links=False,
                        include_comments=False,
                        output_format="txt",
                    ) or ""
                except Exception:
                    pass
            if not text:
                # Fallback: BeautifulSoup accepts bytes and detects charset via lxml
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(raw, "lxml")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = soup.get_text(separator="\n")
                text = _re.sub(r"\n{3,}", "\n\n", text).strip()
            detected_type = "html"
        else:
            text = raw.decode("utf-8", errors="replace")
            detected_type = "text"

        truncated = len(text) > max_chars
        response: dict = {
            "success": True,
            "url": url,
            "content_type": detected_type,
            "text": text[:max_chars],
            "truncated": truncated,
        }
        if tables:
            # Cap table count to avoid blowing up the agent's context window
            response["tables"] = tables[:30]
            response["tables_truncated"] = len(tables) > 30
        return response

    except requests.RequestException as e:
        return {"success": False, "url": url, "error": f"Request failed: {e}"}
    except Exception as e:
        return {"success": False, "url": url, "error": f"Parsing failed: {e}"}


# ── Financial Data: SEC EDGAR (US) ─────────────────────────────────────────────

# SEC requires a descriptive User-Agent with contact info — use a real one.
_SEC_HEADERS = {
    "User-Agent": "Greenly Research admin@greenly.app",
    "Accept": "application/json",
}
_SEC_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
_sec_ticker_cache: dict = {}  # ticker (uppercase) -> {"cik": int, "name": str}


def _load_sec_ticker_map() -> dict:
    """Lazy-load and cache the SEC ticker → CIK map."""
    global _sec_ticker_cache
    if _sec_ticker_cache:
        return _sec_ticker_cache
    try:
        resp = requests.get(_SEC_TICKER_URL, headers=_SEC_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        # The file is keyed by row index, values: {cik_str, ticker, title}
        _sec_ticker_cache = {
            row["ticker"].upper(): {"cik": int(row["cik_str"]), "name": row["title"]}
            for row in data.values()
        }
    except Exception as e:
        logger.error("Failed to load SEC ticker map: %s", e)
    return _sec_ticker_cache


def fetch_filing_us(ticker: str, form_type: str = "10-K", limit: int = 1) -> dict:
    """
    Fetch the most recent SEC filing(s) for a US-listed company by ticker.
    Most authoritative source for US public-company financials.

    form_type examples: "10-K" (annual report), "10-Q" (quarterly), "8-K"
    (material events), "DEF 14A" (proxy), "S-1" (IPO registration).

    Returns a list of filings with `accession_number`, `filing_date`, `report_date`,
    `primary_document_url`, and the extracted `text` + `tables` from the primary
    document. Use a small `limit` (default 1) — each filing pulls a multi-MB doc.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        return {"success": False, "error": "ticker must be a non-empty string"}

    ticker_map = _load_sec_ticker_map()
    if not ticker_map:
        return {"success": False, "error": "Failed to load SEC ticker map"}

    info = ticker_map.get(ticker.strip().upper())
    if not info:
        return {"success": False, "error": f"Ticker '{ticker}' not found in SEC EDGAR registry"}

    cik = info["cik"]
    cik_padded = f"{cik:010d}"

    submissions_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        resp = requests.get(submissions_url, headers=_SEC_HEADERS, timeout=20)
        resp.raise_for_status()
        sub = resp.json()
    except Exception as e:
        return {"success": False, "error": f"SEC submissions fetch failed: {e}"}

    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    primary_docs = recent.get("primaryDocument", [])

    # Match form_type case-insensitively, allowing both "10-K" and "10K"
    target = form_type.strip().upper().replace("-", "")
    matches = [
        i for i, f in enumerate(forms)
        if f and f.upper().replace("-", "") == target
    ][:limit]

    if not matches:
        return {
            "success": False,
            "error": f"No '{form_type}' filings found for {ticker} (CIK {cik})",
            "available_forms": sorted(set(forms))[:20],
        }

    results = []
    for idx in matches:
        accession = accessions[idx]
        accession_clean = accession.replace("-", "")
        primary = primary_docs[idx]
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/{primary}"
        )

        # The primary doc is usually HTML (10-K, 10-Q) — fetch_url handles both
        fetched = fetch_url(doc_url, max_chars=50000, headers=_SEC_HEADERS)

        results.append({
            "accession_number": accession,
            "filing_date": filing_dates[idx],
            "report_date": report_dates[idx],
            "primary_document_url": doc_url,
            "form_type": forms[idx],
            "fetched": fetched,
        })

    logger.info("fetch_filing_us: %s %s → %d filing(s)", ticker, form_type, len(results))
    return {
        "success": True,
        "company_name": info["name"],
        "ticker": ticker.upper(),
        "cik": cik,
        "form_type": form_type,
        "filings": results,
    }


# ── Financial Data: BSE India ──────────────────────────────────────────────────

_BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}


def _is_json_response(resp) -> bool:
    """Return True only if the response Content-Type is JSON (not an HTML block page)."""
    ct = resp.headers.get("Content-Type", "")
    return "json" in ct



def _web_search_annual_report(company_name: str, ticker: str, year: Optional[int]) -> dict:
    """
    Search for an Indian company's annual report PDF directly via web search
    and fetch the first PDF link found.
    """
    year_str = str(year) if year else ""
    query = f"{company_name} {ticker} annual report {year_str} filetype:pdf India investor relations".strip()
    search = web_search_financial(query, region="in", num_results=8, prefer_pdf=True)

    if not search.get("success"):
        return {
            "success": False,
            "error": f"Web search also failed: {search.get('error')}",
            "search_attempted": query,
        }

    results = search.get("results") or []
    for r in results:
        url = r.get("href") or r.get("url") or ""
        if url.lower().endswith(".pdf") or "annualreport" in url.lower() or "annual-report" in url.lower():
            fetched = fetch_url(url, max_chars=50000)
            if fetched.get("success"):
                logger.info("fetch_filing_india: web search found PDF for %s at %s", ticker, url)
                return {
                    "success": True,
                    "source": "web_search_fallback",
                    "company_name": company_name,
                    "ticker": ticker,
                    "pdf_url": url,
                    "fetched": fetched,
                }

    # No direct PDF found — return the search results so the agent can use them
    return {
        "success": True,
        "source": "web_search_fallback",
        "company_name": company_name,
        "ticker": ticker,
        "note": "No direct PDF link found via web search. Use fetch_url on one of the links below to retrieve the report.",
        "search_results": results,
    }


def fetch_filing_india(ticker: str, year: Optional[int] = None) -> dict:
    """
    Fetch the most recent annual report PDF for an Indian (BSE-listed) company
    by ticker symbol or BSE scripcode. Most authoritative source for Indian
    public-company annual reports.

    If `year` is provided, returns the annual report for that fiscal year if
    available; otherwise returns the most recent one. Tries web search first
    (more reliable outside India), then falls back to BSE API.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        return {"success": False, "error": "ticker must be a non-empty string"}

    ticker = ticker.strip().upper()

    # Resolve company name via NSE autocomplete for a better search query
    company_name = ticker
    if not ticker.isdigit():
        try:
            resp = requests.get(
                f"https://www.nseindia.com/api/search/autocomplete?q={ticker}",
                headers=_NSE_HEADERS,
                timeout=10,
            )
            if resp.ok and _is_json_response(resp):
                symbols = resp.json().get("symbols", [])
                if symbols:
                    company_name = symbols[0].get("symbol_info") or ticker
        except Exception as e:
            logger.warning("NSE name lookup failed for %s: %s", ticker, e)

    # Step 1: web search for the annual report PDF (primary — works globally)
    result = _web_search_annual_report(company_name, ticker, year)
    if result.get("success") and result.get("pdf_url"):
        return result

    # Step 2: BSE API (fallback — geo-blocked outside India but try anyway)
    logger.info("fetch_filing_india: web search did not find a direct PDF for %s, trying BSE API", ticker)
    scripcode = ticker if ticker.isdigit() else None
    if not scripcode:
        try:
            resp = requests.get(
                f"https://api.bseindia.com/BseIndiaAPI/api/Type/w?Type=eq&text={ticker}",
                headers=_BSE_HEADERS,
                timeout=15,
            )
            if resp.ok and _is_json_response(resp):
                data = resp.json()
                if data:
                    scripcode = str(data[0].get("scrip_cd") or "").strip() or None
                    company_name = data[0].get("scrip_name") or company_name
        except Exception as e:
            logger.warning("BSE ticker lookup failed for %s: %s", ticker, e)

    if scripcode:
        try:
            resp = requests.get(
                f"https://api.bseindia.com/BseIndiaAPI/api/AnnualReport_New/w?scripcode={scripcode}",
                headers=_BSE_HEADERS,
                timeout=15,
            )
            if resp.ok and _is_json_response(resp):
                reports = resp.json().get("Table", []) or []
                if reports:
                    chosen = None
                    if year is not None:
                        chosen = next(
                            (r for r in reports if str(year) in str(r.get("AR_Year", ""))),
                            None,
                        )
                    if not chosen:
                        chosen = reports[0]
                    pdf_path = (chosen.get("Fld_Attachment") or "").strip()
                    if pdf_path:
                        pdf_url = (
                            pdf_path if pdf_path.startswith("http")
                            else f"https://www.bseindia.com/bseplus/AnnualReport/{scripcode}/{pdf_path}"
                        )
                        fetched = fetch_url(pdf_url, max_chars=50000, headers=_BSE_HEADERS)
                        logger.info("fetch_filing_india: BSE API found report for %s scripcode=%s", ticker, scripcode)
                        return {
                            "success": True,
                            "source": "bse",
                            "company_name": company_name,
                            "ticker": ticker,
                            "scripcode": scripcode,
                            "report_year": chosen.get("AR_Year"),
                            "pdf_url": pdf_url,
                            "fetched": fetched,
                        }
        except Exception as e:
            logger.warning("BSE annual report fetch failed for %s: %s", ticker, e)

    # Return whatever the web search found (may have search_results even without a direct PDF)
    return result


# ── Financial-Tuned Web Search ─────────────────────────────────────────────────

def web_search_financial(
    query: str,
    region: str = "auto",
    num_results: int = 5,
    prefer_pdf: bool = True,
) -> dict:
    """
    Search the web optimised for financial reports and filings.

    region:
      - "us"   → biases toward SEC.gov, EDGAR, US investor-relations pages
      - "in"   → biases toward BSE, NSE, SEBI, Indian investor-relations pages
      - "auto" → detects from query text (default)

    Use this instead of generic web_search when looking for annual reports,
    quarterly filings, earnings releases, or other primary financial sources.
    Combine with fetch_url to read the actual document.
    """
    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": "query must be a non-empty string"}

    boosters = {
        "us": "(annual report OR 10-K OR 10-Q OR investor relations) (site:sec.gov OR inurl:investor)",
        "in": "(annual report OR investor relations) (site:bseindia.com OR site:nseindia.com OR site:sebi.gov.in)",
    }

    if region == "auto":
        ql = query.lower()
        india_signals = ("india", " in ", "nse", "bse", "sebi", "₹", "rupee", "crore", "lakh")
        region = "in" if any(s in ql for s in india_signals) else "us"

    enhanced = f"{query} {boosters.get(region, '')}".strip()
    if prefer_pdf:
        enhanced += " filetype:pdf"

    result = web_search(enhanced, num_results)
    if isinstance(result, dict):
        result["region"] = region
        result["query_used"] = enhanced
    return result


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
    range: Optional[str] = None,
    hasHeader: bool = True,
    _ns: dict = None,
) -> dict:
    """
    Read a Google Sheet tab into a pandas DataFrame and store it in the sandbox
    namespace under varName.

    Loading modes:
    - Default: loads the full used range of the sheet.
    - range: if provided (A1 notation, e.g. 'E1:H50' or 'Sheet1!E1:H50'), loads
      exactly that cell range instead of the full sheet.

    Header behaviour (hasHeader):
    - True (default): first row of the loaded range is used as column names.
      Duplicate / empty headers are de-duplicated (e.g. 'Sales', 'Sales_1').
      Rows wider than the header row get synthesised 'ColN' overflow names.
    - False: no header row is consumed; all rows become data and columns are
      named Col0, Col1, Col2, … Use this when the range contains only numbers
      or when you want to skip header detection entirely.

    Column filtering (columns):
    - Accepts header name strings (e.g. ['Revenue', 'Profit Margin %']).
      NOT spreadsheet column letters like 'A' or 'E'.
    - Only valid when hasHeader=True. Ignored when hasHeader=False.
    - Omit to keep all columns.
    """
    warnings: list = []
    try:
        if range:
            # Use caller-supplied range directly; prepend sheet name if absent
            range_str = range if "!" in range else f"'{sheetTitle}'!{range}"
        else:
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
            return {"success": False, "error": f"No data found in range '{range_str}'"}

        def _cell_val(cell):
            v = cell.get("value")
            return v if v is not None else cell.get("displayValue")

        if hasHeader:
            raw_headers = [_cell_val(cell) for cell in cells[0]]
            data_rows = [[_cell_val(cell) for cell in row] for row in cells[1:]]

            max_row_width = max((len(r) for r in data_rows), default=0)
            header_width = len(raw_headers)
            if max_row_width > header_width:
                extra = max_row_width - header_width
                raw_headers = list(raw_headers) + [None] * extra
                warnings.append(
                    f"{extra} row(s) had more columns than headers; synthesised "
                    f"'Col{header_width}'..'Col{max_row_width - 1}' names for the overflow."
                )

            headers = _dedupe_headers(raw_headers)
        else:
            data_rows = [[_cell_val(cell) for cell in row] for row in cells]
            n_cols = max((len(r) for r in data_rows), default=0)
            headers = [f"Col{i}" for i in range(n_cols)]

        n_cols = len(headers)
        data_rows = [r + [None] * (n_cols - len(r)) for r in data_rows]
        df = pd.DataFrame(data_rows, columns=headers)

        if columns:
            if not hasHeader:
                warnings.append("'columns' filter is ignored when hasHeader=False.")
            else:
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


# ── Skills loader ─────────────────────────────────────────────────────────────

_SKILLS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "skills"))


def load_skill(skill_name: str) -> dict:
    """
    Load the SKILL.md documentation for a named skill from the skills library.
    Call this during planning to understand exactly how to execute a skill
    (e.g. 'DCF' for discounted cash flow analysis). Returns the full markdown
    content of the skill's SKILL.md file, including required inputs, steps,
    and expected outputs. If the skill is not found, returns the list of
    available skills so you can pick the correct name.
    """
    skill_name = skill_name.strip()

    if not skill_name or ".." in skill_name or "/" in skill_name or "\\" in skill_name:
        return {"success": False, "error": f"Invalid skill name: '{skill_name}'"}

    skill_path = os.path.normpath(os.path.join(_SKILLS_DIR, skill_name, "SKILL.md"))

    if not os.path.isfile(skill_path):
        try:
            available = sorted(
                d for d in os.listdir(_SKILLS_DIR)
                if os.path.isdir(os.path.join(_SKILLS_DIR, d))
            )
        except Exception:
            available = []
        return {
            "success": False,
            "error": f"Skill '{skill_name}' not found.",
            "available_skills": available,
        }

    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info("load_skill: loaded '%s' (%d chars)", skill_name, len(content))
        return {"success": True, "skill_name": skill_name, "content": content}
    except Exception as e:
        return {"success": False, "error": f"Failed to read SKILL.md: {e}"}


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
    range: Optional[str] = Field(None, description="A1 notation range to load, e.g. 'E1:H50'. Omit to load the full used range of the sheet. Sheet name prefix is optional.")
    hasHeader: bool = Field(True, description="If True (default), the first row is used as column names. Set False when the range has no header row (e.g. all-numeric data) — columns will be named Col0, Col1, etc.")
    columns: Optional[List[str]] = Field(None, description="Header name strings to keep, e.g. ['Revenue', 'Profit Margin %']. NOT column letters like 'E'. Only applies when hasHeader=True. Omit to keep all columns.")
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
        range: Optional[str] = None,
        hasHeader: bool = True,
    ):
        return json.dumps(
            load_sheet_to_df(service, spreadsheetId, sheetTitle, columns, varName, range, hasHeader, _ns=ns),
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
