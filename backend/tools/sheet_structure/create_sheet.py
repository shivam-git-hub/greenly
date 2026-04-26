import logging
from ..utils import normalize_color

logger = logging.getLogger(__name__)


def create_sheet(
    service,
    spreadsheetId: str,
    title: str,
    index: int = None,
    tabColor: dict = None,
    gridProperties: dict = None,
) -> dict:
    """
    Add a new tab to the workbook at the specified position.
    Returns the numeric sheetId assigned by the Sheets API.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        title: Tab name. Must be unique within the workbook.
        index: 0-based position to insert the tab. Default: appends at end.
        tabColor: RGB color dict {r, g, b} for the tab colour indicator.
        gridProperties: Dict with optional keys:
                        - rowCount (int): number of rows (default: 1000)
                        - columnCount (int): number of columns (default: 26)
    """
    req = {"spreadsheetId": spreadsheetId, "title": title, "index": index}
    logger.info("create_sheet request: %s", req)

    try:
        sheet_props = {"title": title}
        if index is not None:
            sheet_props["index"] = index

        if gridProperties:
            sheet_props["gridProperties"] = {
                "rowCount":    gridProperties.get("rowCount", 1000),
                "columnCount": gridProperties.get("columnCount", 26),
            }

        if tabColor:
            sheet_props["tabColorStyle"] = {"rgbColor": normalize_color(tabColor)}

        result = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={"requests": [{"addSheet": {"properties": sheet_props}}]},
        ).execute()

        props = (
            result.get("replies", [{}])[0]
            .get("addSheet", {})
            .get("properties", {})
        )
        response = {
            "success": True,
            "sheetId": props.get("sheetId"),
            "title":   props.get("title", title),
            "index":   props.get("index", index),
        }
        logger.info("create_sheet response: %s", response)
        return response
    except Exception as e:
        logger.error("Error creating sheet: %s", e)
        return {"success": False, "error": str(e)}


def resize_sheet(
    service,
    spreadsheetId: str,
    sheetId: int,
    rowCount: int,
    columnCount: int,
) -> dict:
    """
    Resize an existing sheet by updating its gridProperties.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetId: The numeric ID of the sheet to resize.
        rowCount: New row count.
        columnCount: New column count.

    Returns:
        dict: {"success": True, "sheetId": ..., "rowCount": ..., "columnCount": ...}
              or {"success": False, "error": ...}
    """
    req = {
        "spreadsheetId": spreadsheetId,
        "sheetId": sheetId,
        "rowCount": rowCount,
        "columnCount": columnCount,
    }
    logger.info("resize_sheet request: %s", req)

    try:
        result = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheetId,
                                "gridProperties": {
                                    "rowCount": rowCount,
                                    "columnCount": columnCount,
                                },
                            },
                            "fields": "gridProperties(rowCount,columnCount)",
                        }
                    }
                ]
            },
        ).execute()

        response = {
            "success": True,
            "sheetId": sheetId,
            "rowCount": rowCount,
            "columnCount": columnCount,
        }
        logger.info("resize_sheet response: %s", response)
        return response
    except Exception as e:
        logger.error("Error resizing sheet: %s", e)
        return {"success": False, "error": str(e)}