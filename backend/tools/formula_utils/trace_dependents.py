import re
import logging
from collections import defaultdict, deque
from ..utils import get_formula, index_to_col_letter

logger = logging.getLogger(__name__)

# Matches 'Sheet Name'!A1:B2  or  SheetName!A1  or  plain A1
_REF_RE = re.compile(
    r"'([^']+)'!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?"
    r"|([A-Za-z0-9_]+)!\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?"
    r"|(?<![A-Za-z!])(\$?[A-Z]+\$?\d+)(?::(\$?[A-Z]+\$?\d+))?"
)

_PLAIN_CELL_RE = re.compile(r"\$?([A-Z]+)\$?(\d+)")


def trace_dependents(
    service,
    spreadsheetId: str,
    cell: str,
    direction: str = "both",
    maxDepth: int = 3,
) -> dict:
    """
    Map the dependency chain of a cell — what it depends on and what depends on it.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        cell: A1 notation of the cell to trace, including sheet name,
              e.g. 'Output!C4'.
        direction: 'precedents' (what feeds into cell), 'dependents' (what
                   cell feeds into), or 'both' (default).
        maxDepth: How many dependency hops to follow. Default: 3. -1 for full chain.

    """
    req = {
        "spreadsheetId": spreadsheetId,
        "cell": cell,
        "direction": direction,
        "maxDepth": maxDepth,
    }
    logger.info("trace_dependents request: %s", req)

    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            includeGridData=True,
            fields="sheets(properties(title,sheetId),data(startRow,startColumn,rowData.values.userEnteredValue.formulaValue))",
        ).execute()

        # Build cell -> formula map for the whole workbook
        all_formulas: dict[str, str] = {}
        for sheet in meta.get("sheets", []):
            sheet_name = sheet["properties"]["title"]
            for grid in sheet.get("data", []):
                start_row = grid.get("startRow", 0)
                start_col = grid.get("startColumn", 0)
                for ri, row_data in enumerate(grid.get("rowData", [])):
                    for ci, cell_data in enumerate(row_data.get("values", [])):
                        formula = get_formula(cell_data)
                        if formula:
                            abs_col = start_col + ci
                            abs_row = start_row + ri
                            a1 = f"{sheet_name}!{index_to_col_letter(abs_col)}{abs_row + 1}"
                            all_formulas[a1] = formula

        # Build adjacency maps
        precedents_map: dict[str, set] = defaultdict(set)   # cell -> cells it references
        dependents_map: dict[str, set] = defaultdict(set)   # cell -> cells that reference it

        for cell_a1, formula in all_formulas.items():
            context_sheet = cell_a1.split("!")[0]
            for ref in _extract_refs(formula, context_sheet):
                precedents_map[cell_a1].add(ref)
                dependents_map[ref].add(cell_a1)

        queried_formula = all_formulas.get(cell)
        result: dict = {"success": True, "cell": cell, "formula": queried_formula}

        if direction in ("precedents", "both"):
            result["precedents"] = _bfs(precedents_map, all_formulas, cell, maxDepth)
        if direction in ("dependents", "both"):
            result["dependents"] = _bfs(dependents_map, all_formulas, cell, maxDepth)

        logger.info(
            "trace_dependents response: %d precedents, %d dependents",
            len(result.get("precedents", [])),
            len(result.get("dependents", [])),
        )
        return result
    except Exception as e:
        logger.error("Error tracing dependents: %s", e)
        return {"success": False, "error": str(e)}


def _bfs(
    graph: dict,
    all_formulas: dict,
    start: str,
    max_depth: int,
) -> list:
    visited = set()
    queue   = deque()
    result  = []

    for neighbour in graph.get(start, set()):
        if neighbour not in visited:
            queue.append((neighbour, 1))

    while queue:
        node, depth = queue.popleft()
        if node in visited:
            continue
        visited.add(node)
        result.append({
            "cell":    node,
            "depth":   depth,
            "formula": all_formulas.get(node),
        })
        if max_depth == -1 or depth < max_depth:
            for neighbour in graph.get(node, set()):
                if neighbour not in visited:
                    queue.append((neighbour, depth + 1))

    return result


_MAX_RANGE_EXPAND = 10_000


def _col_to_index(col: str) -> int:
    result = 0
    for c in col.upper():
        result = result * 26 + (ord(c) - ord("A") + 1)
    return result - 1


def _index_to_col(idx: int) -> str:
    result = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord("A") + rem) + result
    return result


def _expand_range(sheet: str, start_col: str, start_row: str, end_col: str, end_row: str) -> list:
    """Expand 'Sheet!A1:B2' into individual cell references, bounded by _MAX_RANGE_EXPAND."""
    try:
        sc, ec = _col_to_index(start_col), _col_to_index(end_col)
        sr, er = int(start_row), int(end_row)
    except (TypeError, ValueError):
        return [f"{sheet}!{start_col}{start_row}"]
    if sc > ec:
        sc, ec = ec, sc
    if sr > er:
        sr, er = er, sr
    total = (ec - sc + 1) * (er - sr + 1)
    if total > _MAX_RANGE_EXPAND:
        return [f"{sheet}!{start_col}{start_row}:{end_col}{end_row}"]
    return [
        f"{sheet}!{_index_to_col(c)}{r}"
        for r in range(sr, er + 1)
        for c in range(sc, ec + 1)
    ]


def _extract_refs(formula: str, context_sheet: str) -> list:
    """Extract canonical 'Sheet!CellAddress' strings from a formula.
    Ranges (A1:B2) are expanded into their member cells so dependency graphs
    capture every affected cell, not just the range's top-left corner."""
    refs = []
    for m in _REF_RE.finditer(formula):
        if m.group(1):                         # 'Sheet Name'!A1 or 'Sheet Name'!A1:B2
            sheet = m.group(1)
            col, row = m.group(2), m.group(3)
            end_col, end_row = m.group(4), m.group(5)
            if end_col and end_row:
                refs.extend(_expand_range(sheet, col, row, end_col, end_row))
            else:
                refs.append(f"{sheet}!{col}{row}")
        elif m.group(6):                        # SheetName!A1 or SheetName!A1:B2
            sheet = m.group(6)
            col, row = m.group(7), m.group(8)
            end_col, end_row = m.group(9), m.group(10)
            if end_col and end_row:
                refs.extend(_expand_range(sheet, col, row, end_col, end_row))
            else:
                refs.append(f"{sheet}!{col}{row}")
        elif m.group(11) and context_sheet:     # plain A1 or A1:B2 — qualify with context sheet
            start_raw = m.group(11).replace("$", "")
            end_raw = m.group(12).replace("$", "") if m.group(12) else None
            if end_raw:
                sm = _PLAIN_CELL_RE.fullmatch(start_raw)
                em = _PLAIN_CELL_RE.fullmatch(end_raw)
                if sm and em:
                    refs.extend(_expand_range(context_sheet, sm.group(1), sm.group(2), em.group(1), em.group(2)))
                else:
                    refs.append(f"{context_sheet}!{start_raw}")
            else:
                refs.append(f"{context_sheet}!{start_raw}")
    return refs
