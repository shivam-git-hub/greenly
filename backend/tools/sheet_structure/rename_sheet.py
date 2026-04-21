import logging

logger = logging.getLogger(__name__)


def rename_sheet(
    service,
    spreadsheetId: str,
    sheetId: int,
    newTitle: str,
) -> dict:
    """
    Rename an existing tab in the workbook.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetId: Numeric sheet ID (from read_sheet_structure, not the tab name).
        newTitle: New tab name. Must be unique within the workbook.
    """
    logger.info("rename_sheet: spreadsheetId=%s sheetId=%d newTitle=%s", spreadsheetId, sheetId, newTitle)

    try:
        result = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheetId,
                                "title": newTitle,
                            },
                            "fields": "title",
                        }
                    }
                ]
            },
        ).execute()

        response = {
            "success": True,
            "sheetId": sheetId,
            "newTitle": newTitle,
        }
        logger.info("rename_sheet response: %s", response)
        return response
    except Exception as e:
        logger.error("Error renaming sheet: %s", e)
        return {"success": False, "error": str(e)}
