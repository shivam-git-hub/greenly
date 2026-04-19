import logging
from ..utils import parse_a1_range, get_sheet_id_by_name, index_to_col_letter

logger = logging.getLogger(__name__)


def create_named_range(
    service, spreadsheetId: str, name: str, range: str
) -> dict:
    """
    Register a workbook-level named range.
    Args:
        spreadsheetId: The ID of the target spreadsheet.
        name: Unique name. No spaces — use underscores, e.g. 'WACC_Rate'.
        range: A1 notation including sheet name, e.g. 'Assumptions!C18'.

    """
    req = {"spreadsheetId": spreadsheetId, "name": name, "range": range}
    logger.info("create_named_range request: %s", req)

    try:
        parsed   = parse_a1_range(range)
        sheet_id = get_sheet_id_by_name(service, spreadsheetId, parsed["sheet"])

        grid_range = {
            "sheetId":          sheet_id,
            "startRowIndex":    parsed["startRowIndex"],
            "endRowIndex":      parsed["endRowIndex"],
            "startColumnIndex": parsed["startColumnIndex"],
            "endColumnIndex":   parsed["endColumnIndex"],
        }
        # Strip None values for unbounded ranges
        grid_range = {k: v for k, v in grid_range.items() if v is not None}

        result = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "addNamedRange": {
                            "namedRange": {
                                "name": name,
                                "range": grid_range,
                            }
                        }
                    }
                ]
            },
        ).execute()

        named_range_id = (
            result.get("replies", [{}])[0]
            .get("addNamedRange", {})
            .get("namedRange", {})
            .get("namedRangeId")
        )
        response = {"success": True, "namedRangeId": named_range_id, "name": name}
        logger.info("create_named_range response: %s", response)
        return response
    except Exception as e:
        logger.error("Error creating named range: %s", e)
        return {"success": False, "error": str(e)}


def get_named_ranges(service, spreadsheetId: str) -> dict:
    """
    List all named ranges in the workbook with their IDs and resolved cell ranges.

    Args:
        spreadsheetId: The ID of the target spreadsheet.

    """
    req = {"spreadsheetId": spreadsheetId}
    logger.info("get_named_ranges request: %s", req)

    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            fields="namedRanges,sheets.properties",
        ).execute()

        sheets_map = {
            s["properties"]["sheetId"]: s["properties"]["title"]
            for s in meta.get("sheets", [])
        }

        named_ranges = []
        for nr in meta.get("namedRanges", []):
            r      = nr.get("range", {})
            sid    = r.get("sheetId", 0)
            sname  = sheets_map.get(sid, str(sid))
            
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

        response = {"success": True, "namedRanges": named_ranges}
        logger.info("get_named_ranges response: %d named ranges", len(named_ranges))
        return response
    except Exception as e:
        logger.error("Error getting named ranges: %s", e)
        return {"success": False, "error": str(e)}


def delete_named_range(service, spreadsheetId: str, namedRangeId: str) -> dict:
    """
    Remove a named range from the workbook by its ID.
    Any formula that references the deleted name immediately evaluates to a
    #NAME? error. Always audit dependent formulas before deleting.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        namedRangeId: ID from get_named_ranges.
    """
    req = {"spreadsheetId": spreadsheetId, "namedRangeId": namedRangeId}
    logger.info("delete_named_range request: %s", req)

    try:
        # Resolve the human-readable name before deletion for the response
        existing     = get_named_ranges(service, spreadsheetId)
        deleted_name = next(
            (nr["name"] for nr in existing.get("namedRanges", []) if nr["namedRangeId"] == namedRangeId),
            namedRangeId,
        )

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={"requests": [{"deleteNamedRange": {"namedRangeId": namedRangeId}}]},
        ).execute()

        response = {"success": True, "deletedName": deleted_name}
        logger.info("delete_named_range response: %s", response)
        return response
    except Exception as e:
        logger.error("Error deleting named range: %s", e)
        return {"success": False, "error": str(e)}
