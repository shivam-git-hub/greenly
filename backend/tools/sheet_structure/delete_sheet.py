import logging

logger = logging.getLogger(__name__)


def delete_sheet(
    service,
    spreadsheetId: str,
    sheetId: int,
) -> dict:
    """
    Permanently delete a tab from the workbook. This cannot be undone.
    A spreadsheet must always have at least one sheet — the API will reject
    deletion of the last remaining tab.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetId: Numeric sheet ID (from read_sheet_structure, not the tab name).
    """
    logger.info("delete_sheet: spreadsheetId=%s sheetId=%d", spreadsheetId, sheetId)

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {"deleteSheet": {"sheetId": sheetId}}
                ]
            },
        ).execute()

        response = {"success": True, "sheetId": sheetId}
        logger.info("delete_sheet response: %s", response)
        return response
    except Exception as e:
        logger.error("Error deleting sheet: %s", e)
        return {"success": False, "error": str(e)}
