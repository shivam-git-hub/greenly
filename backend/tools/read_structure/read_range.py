import logging
from ..utils import get_effective_value, get_formula

logger = logging.getLogger(__name__)


def read_range(service, spreadsheetId: str, range: str) -> dict:
    """
    Read values, display strings, and formula strings from a cell range.
    Maximum 2000 rows per call; use get_chunk for larger datasets.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        range: A1 notation range, e.g. 'Model!B8:D8'.

    """
    req = {"spreadsheetId": spreadsheetId, "range": range}
    logger.info("read_range request: %s", req)

    try:
        result = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            ranges=[range],
            includeGridData=True,
            fields="sheets.data.rowData.values(userEnteredValue,effectiveValue,formattedValue)",
        ).execute()

        _MAX_ROWS = 30
        cells = []
        for sheet_data in result.get("sheets", []):
            for grid_data in sheet_data.get("data", []):
                for row_data in grid_data.get("rowData", []):
                    if len(cells) >= _MAX_ROWS:
                        break
                    row = [
                        {
                            "value":        get_effective_value(cell),
                            "formula":      get_formula(cell),
                            "displayValue": cell.get("formattedValue"),
                        }
                        for cell in row_data.get("values", [])
                    ]
                    cells.append(row)

        truncated = len(cells) == _MAX_ROWS
        response = {
            "success":   True,
            "range":     range,
            "cells":     cells,
            **({"warning": f"Response capped at {_MAX_ROWS} rows. Use get_chunk for full data."} if truncated else {}),
        }
        logger.info("read_range response: range=%s rows=%d%s", range, len(cells), " (capped)" if truncated else "")
        return response
    except Exception as e:
        logger.error("Error reading range: %s", e)
        return {"success": False, "error": str(e)}
