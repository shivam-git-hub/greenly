import logging
from ..sheet_structure.create_sheet import resize_sheet
from ..utils import parse_a1_range

logger = logging.getLogger(__name__)


def _get_range_dimensions(range_str: str) -> tuple:
    """
    Parse A1 notation to get required row and column counts.

    Args:
        range_str: e.g. 'Sheet1!A1:AD8' or 'A1:AD8'

    Returns:
        (required_rows, required_cols) - 1-based counts needed
    """
    parsed = parse_a1_range(range_str)

    start_row = parsed.get("startRowIndex", 0) or 0
    start_col = parsed.get("startColumnIndex", 0) or 0
    end_row = parsed.get("endRowIndex", start_row + 1) or (start_row + 1)
    end_col = parsed.get("endColumnIndex", start_col + 1) or (start_col + 1)

    required_rows = end_row
    required_cols = end_col

    return required_rows, required_cols


def _ensure_grid_expansion(service, spreadsheetId: str, range_str: str) -> bool:
    """
    Check if the range exceeds sheet grid limits and auto-expand if needed.

    Returns True if expansion was performed or was not needed.
    Returns False if expansion failed.
    """
    parsed = parse_a1_range(range_str)
    sheet_name = parsed.get("sheet", "")

    required_rows, required_cols = _get_range_dimensions(range_str)

    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))",
        ).execute()
    except Exception as e:
        logger.error("Error fetching sheet metadata: %s", e)
        return False

    sheets = meta.get("sheets", [])
    target_sheet = None
    for sheet in sheets:
        props = sheet.get("properties", {})
        if sheet_name and props.get("title") == sheet_name:
            target_sheet = sheet
            break
        elif not sheet_name:
            target_sheet = sheet

    if target_sheet:
        props = target_sheet.get("properties", {})
        sheet_id = props.get("sheetId")
        grid_props = props.get("gridProperties", {})
        current_rows = grid_props.get("rowCount", 1000)
        current_cols = grid_props.get("columnCount", 26)

        if required_rows <= current_rows and required_cols <= current_cols:
            return True

        new_rows = max(required_rows, current_rows)
        new_cols = max(required_cols, current_cols)

        logger.info(
            "Auto-expanding sheet '%s' from %dx%d to %dx%d",
            props.get("title", "default"), current_rows, current_cols, new_rows, new_cols
        )

        resize_result = resize_sheet(
            service=service,
            spreadsheetId=spreadsheetId,
            sheetId=sheet_id,
            rowCount=new_rows,
            columnCount=new_cols,
        )

        if resize_result.get("success"):
            return True
        else:
            logger.error("Failed to resize sheet: %s", resize_result.get("error"))
            return False

    logger.error("No sheet found to expand")
    return False


def write_values(
    service,
    spreadsheetId: str,
    range: str,
    values: list,
    valueInputOption: str = "USER_ENTERED",
) -> dict:
    """
    Write raw values or formulas to a cell range.

    Args:
        spreadsheetId: The ID of the target spreadsheet (from its URL).
        range: A1 notation range, e.g. 'Assumptions!B4:D6'. Must match the
               dimensions of the values array.
        values: 2-D list of cell values. Outer list = rows, inner = columns.
                Use None to skip a cell and leave it unchanged. Strings starting
                with '=' will be evaluated as formulas if valueInputOption is
                USER_ENTERED.
        valueInputOption: 'RAW' stores values exactly as given. 'USER_ENTERED'
                          (default) parses dates, currency, percentages, and formulas.

    """
    req = {
        "spreadsheetId": spreadsheetId,
        "range": range,
        "valueInputOption": valueInputOption,
        "values": values,
    }
    logger.info("write_values request: %s", req)

    try:
        if not _ensure_grid_expansion(service, spreadsheetId, range):
            return {
                "success": False,
                "error": "Failed to expand sheet grid to accommodate the write range",
            }

        result = (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheetId,
                range=range,
                valueInputOption=valueInputOption,
                includeValuesInResponse=True,
                responseValueRenderOption="UNFORMATTED_VALUE",
                body={"values": values},
            )
            .execute()
        )
    except Exception as e:
        logger.error("Error writing values: %s", e)
        return {"success": False, "error": str(e)}

    updated_data = result.get("updatedData", {})
    calculated_values = updated_data.get("values", [])

    has_formulas = any(
        isinstance(raw_val, str) and raw_val.startswith("=")
        for row in values
        for raw_val in (row or [])
    )

    if has_formulas:
        formula_cells = []
        for r_idx, row in enumerate(values):
            for c_idx, raw_val in enumerate(row or []):
                if isinstance(raw_val, str) and raw_val.startswith("="):
                    calc_val = ""
                    if r_idx < len(calculated_values) and c_idx < len(calculated_values[r_idx]):
                        calc_val = calculated_values[r_idx][c_idx]
                    formula_cells.append({
                        "row_index": r_idx,
                        "col_index": c_idx,
                        "calculated_value": calc_val,
                    })

        response = {
            "success": True,
            "spreadsheetId": result.get("spreadsheetId"),
            "updatedRange": result.get("updatedRange"),
            "formulaCells": formula_cells,
        }
    else:
        response = {
            "success": True,
            "updatedRange": result.get("updatedRange"),
        }

    logger.info("write_values response: %s", response)
    return response