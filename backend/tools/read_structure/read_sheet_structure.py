import logging
from collections import deque
import re
from ..utils import index_to_col_letter, col_letter_to_index

logger = logging.getLogger(__name__)


def _cell_text(cell: dict):
    uev = cell.get("userEnteredValue", {})
    if not uev:
        return None
    if "stringValue" in uev:
        v = uev["stringValue"]
        return v if v != "" else None
    if "numberValue" in uev:
        v = uev["numberValue"]
        return str(int(v)) if v == int(v) else str(v)
    if "boolValue" in uev:
        return str(uev["boolValue"])
    if "formulaValue" in uev:
        return uev["formulaValue"]
    return None


def _find_data_blocks(sheet: dict) -> list:
    occupied = {}
    for grid_data in sheet.get("data", []):
        base_r = grid_data.get("startRow", 0)
        base_c = grid_data.get("startColumn", 0)
        for dr, row_data in enumerate(grid_data.get("rowData", [])):
            for dc, cell in enumerate(row_data.get("values", [])):
                val = _cell_text(cell)
                if val is not None:
                    occupied[(base_r + dr, base_c + dc)] = val

    if not occupied:
        return []

    visited = set()
    blocks = []

    for seed in occupied:
        if seed in visited:
            continue
        component = set()
        queue = deque([seed])
        while queue:
            pos = queue.popleft()
            if pos in visited or pos not in occupied:
                continue
            visited.add(pos)
            component.add(pos)
            r, c = pos
            for nb in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if nb not in visited and nb in occupied:
                    queue.append(nb)

        min_r = min(r for r, c in component)
        max_r = max(r for r, c in component)
        min_c = min(c for r, c in component)
        max_c = max(c for r, c in component)

        header_names = [
            occupied.get((min_r, c)) for c in range(min_c, max_c + 1)
        ]

        blocks.append({
            "range":        f"{index_to_col_letter(min_c)}{min_r + 1}:{index_to_col_letter(max_c)}{max_r + 1}",
            "row_count":    max_r - min_r + 1,
            "column_count": max_c - min_c + 1,
            "header_names": header_names,
        })

    blocks.sort(key=lambda b: (int(b["range"].split(":")[0][1:] or 0), b["range"][0]))
    return blocks


def read_sheet_structure(service, spreadsheetId: str) -> dict:
    """
    Fetches all tab names and IDs, the used data range of each tab, named
    ranges, formula count per tab, and data blocks (connected components of
    non-empty cells) with column_count, row_count, and header_names.
    Also used to check for tab name collisions before creating new sheets.

    Args:
        spreadsheetId: The ID of the target spreadsheet.

    """
    req = {"spreadsheetId": spreadsheetId}
    logger.info("read_sheet_structure request: %s", req)

    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            includeGridData=True,
            fields=(
                "spreadsheetId,"
                "properties.title,"
                "sheets(properties,data(startRow,startColumn,rowData.values.userEnteredValue)),"
                "namedRanges"
            ),
        ).execute()

        sheets_summary = []
        for sheet in meta.get("sheets", []):
            props = sheet["properties"]

            formula_count = 0
            for grid_data in sheet.get("data", []):
                for row_data in grid_data.get("rowData", []):
                    for cell in row_data.get("values", []):
                        if cell.get("userEnteredValue", {}).get("formulaValue"):
                            formula_count += 1

            blocks = _find_data_blocks(sheet)

            # Derive usedRange from actual data blocks, NOT gridProperties.rowCount /
            # columnCount. gridProperties reflects sheet grid capacity (default 1000 rows)
            # not where data ends, which causes agents to request ranges far beyond real data.
            if blocks:
                max_row = max_col = 0
                for b in blocks:
                    end_cell = b["range"].split(":")[1]
                    m = re.match(r"([A-Z]+)(\d+)", end_cell)
                    if m:
                        max_col = max(max_col, col_letter_to_index(m.group(1)))
                        max_row = max(max_row, int(m.group(2)))
                used_range = f"A1:{index_to_col_letter(max_col)}{max_row}"
            else:
                used_range = "empty"

            sheets_summary.append({
                "sheetId":      props["sheetId"],
                "title":        props["title"],
                "index":        props["index"],
                "usedRange":    used_range,
                "formulaCount": formula_count,
                "dataBlocks":   blocks,
            })

        sheets_map = {s["sheetId"]: s["title"] for s in sheets_summary}
        named_ranges = []
        for nr in meta.get("namedRanges", []):
            r    = nr.get("range", {})
            sid  = r.get("sheetId", 0)
            sname = sheets_map.get(sid, str(sid))
            
            sc     = r.get("startColumnIndex", 0)
            ec_val = r.get("endColumnIndex")
            ec     = ec_val - 1 if ec_val is not None else sc

            sr_val = r.get("startRowIndex")
            sr     = sr_val + 1 if sr_val is not None else 1
            
            er     = r.get("endRowIndex")
            
            safe_sname = f"'{sname.replace(chr(39), chr(39)+chr(39))}'" if " " in sname or not sname.isalnum() else sname
            
            if er is not None:
                a1 = f"{safe_sname}!{index_to_col_letter(sc)}{sr}:{index_to_col_letter(ec)}{er}"
            else:
                a1 = f"{safe_sname}!{index_to_col_letter(sc)}{sr}:{index_to_col_letter(ec)}"

            named_ranges.append({
                "namedRangeId": nr.get("namedRangeId", ""),
                "name":         nr.get("name", ""),
                "range":        a1,
            })

        response = {
            "success":       True,
            "spreadsheetId": meta.get("spreadsheetId"),
            "title":         meta.get("properties", {}).get("title"),
            "sheets":        sheets_summary,
            "namedRanges":   named_ranges,
        }
        # Per-sheet detail so we can spot at a glance whether the structure tool
        # is correctly reporting an empty sheet vs one full of data.
        for s in sheets_summary:
            logger.info(
                "  sheet[%s]: usedRange=%s blocks=%d formulas=%d",
                s["title"], s["usedRange"], len(s["dataBlocks"]), s["formulaCount"],
            )
        logger.info(
            "read_sheet_structure response: %d sheets, %d named ranges",
            len(sheets_summary), len(named_ranges),
        )
        return response
    except Exception as e:
        logger.error("Error reading sheet structure: %s", e)
        return {"success": False, "error": str(e)}
