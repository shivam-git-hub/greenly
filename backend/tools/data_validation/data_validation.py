import logging
from ..utils import a1_to_grid_range, parse_a1_range, get_sheet_id_by_name, index_to_col_letter

logger = logging.getLogger(__name__)


def set_data_validation(
    service,
    spreadsheetId: str,
    range: str,
    conditionType: str,
    values: list = None,
    formula: str = None,
    showDropdown: bool = True,
    strict: bool = True,
    inputMessage: str = None,
    errorMessage: str = None,
) -> dict:
    """
    Add or replace a data validation rule on a cell range.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        range: A1 notation range to apply validation to, e.g. 'Assumptions!B2'.
        conditionType: ONE_OF_LIST, NUMBER_BETWEEN, NUMBER_GREATER, NUMBER_LESS,
                       DATE_IS_VALID, DATE_BEFORE, DATE_AFTER, TEXT_CONTAINS,
                       TEXT_NOT_CONTAINS, CUSTOM_FORMULA, or BOOLEAN.
        values: Allowed values for ONE_OF_LIST. Date strings for DATE_* conditions.
                Numbers for NUMBER_* conditions.
        formula: Boolean formula for CUSTOM_FORMULA, e.g. '=ISNUMBER(A1)'.
                 Must start with '='.
        showDropdown: Show dropdown arrow for ONE_OF_LIST. Default: True.
        strict: If True (default), rejects invalid input. If False, shows warning
                but allows entry.
        inputMessage: Tooltip shown when cell is selected.
        errorMessage: Error message shown when invalid data is entered.
    """
    req = {"spreadsheetId": spreadsheetId, "range": range, "conditionType": conditionType}
    logger.info("set_data_validation request: %s", req)

    try:
        parsed   = parse_a1_range(range)
        sheet_id = get_sheet_id_by_name(service, spreadsheetId, parsed["sheet"])

        if conditionType == "CUSTOM_FORMULA":
            condition_values = [{"userEnteredValue": formula or ""}]
        else:
            def _format_value(v):
                # Sheets API expects userEnteredValue as a string, but wants
                # numbers/booleans serialised without Python's float quirks
                # (e.g. 1.0 → "1", not "1.0"). Dates pass through as-is.
                if isinstance(v, bool):
                    return "TRUE" if v else "FALSE"
                if isinstance(v, float) and v.is_integer():
                    return str(int(v))
                return str(v)
            condition_values = [{"userEnteredValue": _format_value(v)} for v in (values or [])]

        rule = {
            "condition": {
                "type":   conditionType,
                "values": condition_values,
            },
            "strict":        strict,
            "showCustomUi":  showDropdown,
        }
        if inputMessage:
            rule["inputMessage"] = inputMessage
        if errorMessage:
            rule["errorMessage"] = errorMessage

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "setDataValidation": {
                            "range": a1_to_grid_range(range, sheet_id),
                            "rule":  rule,
                        }
                    }
                ]
            },
        ).execute()

        response = {"success": True, "range": range}
        logger.info("set_data_validation response: %s", response)
        return response
    except Exception as e:
        logger.error("Error setting data validation: %s", e)
        return {"success": False, "error": str(e)}


def get_data_validations(service, spreadsheetId: str, sheetTitle: str) -> dict:
    """
    List all data validation rules on a sheet.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetTitle: Tab name to scan for validation rules, e.g. 'Assumptions'.

    """
    req = {"spreadsheetId": spreadsheetId, "sheetTitle": sheetTitle}
    logger.info("get_data_validations request: %s", req)

    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            ranges=[sheetTitle],
            includeGridData=True,
            fields="sheets(properties(title,sheetId),data.rowData.values.dataValidation)",
        ).execute()
    except Exception as e:
        logger.error("Error getting data validations: %s", e)
        return {"success": False, "error": str(e)}

    rules = []
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] != sheetTitle:
            continue
        for sheet_data in sheet.get("data", []):
            start_row = sheet_data.get("startRow", 0)
            start_col = sheet_data.get("startColumn", 0)
            for ri, row_data in enumerate(sheet_data.get("rowData", [])):
                for ci, cell in enumerate(row_data.get("values", [])):
                    dv = cell.get("dataValidation")
                    if not dv:
                        continue
                    cond  = dv.get("condition", {})
                    ctype = cond.get("type", "")
                    vals  = [v.get("userEnteredValue") for v in cond.get("values", [])]
                    safe_sheet = f"'{sheetTitle.replace(chr(39), chr(39)+chr(39))}'"
                    rules.append({
                        "range":         f"{safe_sheet}!{index_to_col_letter(start_col + ci)}{start_row + ri + 1}",
                        "conditionType": ctype,
                        "values":        vals,
                        "strict":        dv.get("strict", True),
                        "showDropdown":  dv.get("showCustomUi", True),
                    })

    response = {"success": True, "sheetTitle": sheetTitle, "validationRules": rules}
    logger.info("get_data_validations response: %d rules", len(rules))
    return response


def clear_data_validation(service, spreadsheetId: str, range: str) -> dict:
    """
    Remove the data validation rule from a range.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        range: A1 notation range from get_data_validations, e.g. 'Assumptions!B2'.

    """
    req = {"spreadsheetId": spreadsheetId, "range": range}
    logger.info("clear_data_validation request: %s", req)

    try:
        parsed   = parse_a1_range(range)
        sheet_id = get_sheet_id_by_name(service, spreadsheetId, parsed["sheet"])

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {"setDataValidation": {"range": a1_to_grid_range(range, sheet_id)}}
                ]
            },
        ).execute()

        response = {"success": True, "range": range}
        logger.info("clear_data_validation response: %s", response)
        return response
    except Exception as e:
        logger.error("Error clearing data validation: %s", e)
        return {"success": False, "error": str(e)}
