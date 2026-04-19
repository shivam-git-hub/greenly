import logging
from ..utils import a1_to_grid_range, get_sheet_id_by_name, parse_a1_range

logger = logging.getLogger(__name__)

_ALL_FORMAT_FIELDS = (
    "userEnteredFormat.numberFormat,"
    "userEnteredFormat.backgroundColor,"
    "userEnteredFormat.textFormat,"
    "userEnteredFormat.horizontalAlignment,"
    "userEnteredFormat.verticalAlignment,"
    "userEnteredFormat.wrapStrategy,"
    "userEnteredFormat.borders,"
    "userEnteredFormat.padding"
)


def clear_cell_format(
    service,
    spreadsheetId: str,
    range: str,
    fields: str = None,
) -> dict:
    """
    Reset all formatting on a range to sheet defaults.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        range: A1 notation range to clear formatting on, e.g. 'Output!A1:Z50'.
        fields: Optional comma-separated list of format fields to clear.
                Default: clears everything. Example: 'numberFormat,backgroundColor'.
    """
    req = {"spreadsheetId": spreadsheetId, "range": range, "fields": fields}
    logger.info("clear_cell_format request: %s", req)

    try:
        sheet_name = parse_a1_range(range)["sheet"]
        sheet_id   = get_sheet_id_by_name(service, spreadsheetId, sheet_name)
        
        if fields:
            mask_parts = []
            for f in fields.split(","):
                f = f.strip()
                if f and not f.startswith("userEnteredFormat."):
                    mask_parts.append(f"userEnteredFormat.{f}")
                elif f:
                    mask_parts.append(f)
            mask = ",".join(mask_parts)
        else:
            mask = _ALL_FORMAT_FIELDS

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range":  a1_to_grid_range(range, sheet_id),
                            "cell":   {"userEnteredFormat": {}},
                            "fields": mask,
                        }
                    }
                ]
            },
        ).execute()

        response = {"success": True, "updatedRange": range}
        logger.info("clear_cell_format response: %s", response)
        return response
    except Exception as e:
        logger.error("Error clearing cell format: %s", e)
        return {"success": False, "error": str(e)}
