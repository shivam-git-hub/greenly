import logging

logger = logging.getLogger(__name__)


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

    cell_results = []
    for r_idx, row in enumerate(values):
        if not row:
            continue
        for c_idx, raw_val in enumerate(row):
            calc_val = ""
            if r_idx < len(calculated_values) and c_idx < len(calculated_values[r_idx]):
                calc_val = calculated_values[r_idx][c_idx]

            is_formula = isinstance(raw_val, str) and raw_val.startswith("=")
            
            cell_results.append({
                "row_index": r_idx,
                "col_index": c_idx,
                "raw_input": raw_val,
                "calculated_value": calc_val if is_formula else "",
            })

    response = {
        "success": True,
        "spreadsheetId": result.get("spreadsheetId"),
        "updatedRange":   result.get("updatedRange"),
        "updatedRows":    result.get("updatedRows", 0),
        "updatedColumns": result.get("updatedColumns", 0),
        "updatedCells":   result.get("updatedCells", 0),
        "cellResults":    cell_results,
    }
    logger.info("write_values response: %s", response)
    return response