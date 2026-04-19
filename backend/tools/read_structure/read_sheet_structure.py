import logging
from ..utils import index_to_col_letter

logger = logging.getLogger(__name__)


def read_sheet_structure(service, spreadsheetId: str) -> dict:
    """
    Fetches all tab names and IDs, the used data range of each tab, named
    ranges, and an actual formula count per tab
    Also used to check for tab name collisions before creating new sheets.

    Args:
        spreadsheetId: The ID of the target spreadsheet.

    """
    req = {"spreadsheetId": spreadsheetId}
    logger.info("read_sheet_structure request: %s", req)

    try:
        # Fetch full grid data so we can count formula cells accurately.
        # fields filter keeps the payload small by excluding effectiveValue /
        # formattedValue — we only need userEnteredValue.formulaValue.
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            includeGridData=True,
            fields=(
                "spreadsheetId,"
                "properties.title,"
                "sheets(properties,data.rowData.values.userEnteredValue.formulaValue),"
                "namedRanges"
            ),
        ).execute()

        sheets_summary = []
        for sheet in meta.get("sheets", []):
            props = sheet["properties"]
            grid  = props.get("gridProperties", {})
            row_count = grid.get("rowCount", 0)
            col_count = grid.get("columnCount", 0)
            used_range = (
                f"A1:{index_to_col_letter(col_count - 1)}{row_count}"
                if row_count and col_count
                else "A1"
            )

            # Count cells that actually contain a formula from the fetched grid data
            formula_count = 0
            for grid_data in sheet.get("data", []):
                for row_data in grid_data.get("rowData", []):
                    for cell in row_data.get("values", []):
                        if cell.get("userEnteredValue", {}).get("formulaValue"):
                            formula_count += 1

            sheets_summary.append({
                "sheetId":      props["sheetId"],
                "title":        props["title"],
                "index":        props["index"],
                "usedRange":    used_range,
                "formulaCount": formula_count,
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
        logger.info(
            "read_sheet_structure response: %d sheets, %d named ranges",
            len(sheets_summary), len(named_ranges),
        )
        return response
    except Exception as e:
        logger.error("Error reading sheet structure: %s", e)
        return {"success": False, "error": str(e)}
