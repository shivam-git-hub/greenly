import logging

logger = logging.getLogger(__name__)


def get_cell_format(service, spreadsheetId: str, range: str) -> dict:
    """
    Read the current formatting properties of a cell range.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        range: A1 notation range, e.g. 'Model!B7:B7'.
    """
    req = {"spreadsheetId": spreadsheetId, "range": range}
    logger.info("get_cell_format request: %s", req)

    try:
        result = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            ranges=[range],
            includeGridData=True,
            fields="sheets.data.rowData.values.userEnteredFormat",
        ).execute()

        fmt = {}
        for sheet in result.get("sheets", []):
            for grid in sheet.get("data", []):
                rows = grid.get("rowData", [])
                if not rows:
                    continue
                cells = rows[0].get("values", [])
                if not cells:
                    continue
                raw = cells[0].get("userEnteredFormat", {})
                nf  = raw.get("numberFormat", {})
                bg  = raw.get("backgroundColor", {})
                tf  = raw.get("textFormat", {})
                fg  = tf.get("foregroundColor", {})
                fmt = {
                    "numberFormat": {
                        "type":    nf.get("type"),
                        "pattern": nf.get("pattern"),
                    },
                    "backgroundColor": {
                        "r": bg.get("red", 0),
                        "g": bg.get("green", 0),
                        "b": bg.get("blue", 0),
                    },
                    "textColor": {
                        "r": fg.get("red", 0),
                        "g": fg.get("green", 0),
                        "b": fg.get("blue", 0),
                    },
                    "bold":                 tf.get("bold", False),
                    "italic":               tf.get("italic", False),
                    "fontSize":             tf.get("fontSize", 10),
                    "horizontalAlignment":  raw.get("horizontalAlignment", "LEFT"),
                }
                break
            if fmt:
                break

        response = {"success": True, "range": range, "format": fmt}
        logger.info("get_cell_format response: %s", response)
        return response
    except Exception as e:
        logger.error("Error getting cell format: %s", e)
        return {"success": False, "error": str(e)}
