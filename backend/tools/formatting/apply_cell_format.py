import logging
from ..utils import a1_to_grid_range, get_sheet_id_by_name, normalize_color, parse_a1_range

logger = logging.getLogger(__name__)


def apply_cell_format(
    service,
    spreadsheetId: str,
    range: str,
    numberFormat: dict = None,
    backgroundColor: dict = None,
    textColor: dict = None,
    bold: bool = None,
    italic: bool = None,
    fontSize: int = None,
    horizontalAlignment: str = None,
    verticalAlignment: str = None,
    wrapStrategy: str = None,
    borders: dict = None,
) -> dict:
    """
    Apply static cell formatting to a range using a RepeatCellRequest.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        range: A1 notation range to format, e.g. 'Model!B7:E7'.
        numberFormat: Dict with 'type' (CURRENCY, PERCENT, DATE, TEXT, NUMBER)
                      and 'pattern' string, e.g. {"type": "CURRENCY",
                      "pattern": '"$"#,##0.00_);("$"#,##0.00)'}.
        backgroundColor: RGB color dict {r, g, b} or {red, green, blue}, 0.0-1.0.
        textColor: RGB color dict for font color.
        bold: Set font bold.
        italic: Set font italic.
        fontSize: Font size in points.
        horizontalAlignment: 'LEFT', 'CENTER', or 'RIGHT'.
        verticalAlignment: 'TOP', 'MIDDLE', or 'BOTTOM'.
        wrapStrategy: 'OVERFLOW_CELL', 'WRAP', or 'CLIP'.
        borders: Dict with top/bottom/left/right keys, each:
                 {"style": "SOLID"|"DASHED"|"NONE", "color": {r,g,b}}.
    """
    req = {"spreadsheetId": spreadsheetId, "range": range}
    logger.info("apply_cell_format request: %s", req)

    try:
        sheet_name = parse_a1_range(range)["sheet"]
        sheet_id   = get_sheet_id_by_name(service, spreadsheetId, sheet_name)

        cell_format = {}
        fields = []

        if numberFormat is not None:
            cell_format["numberFormat"] = numberFormat
            fields.append("userEnteredFormat.numberFormat")

        if backgroundColor is not None:
            cell_format["backgroundColor"] = normalize_color(backgroundColor)
            fields.append("userEnteredFormat.backgroundColor")

        text_format = {}
        if textColor is not None:
            text_format["foregroundColor"] = normalize_color(textColor)
            fields.append("userEnteredFormat.textFormat.foregroundColor")
        if bold is not None:
            text_format["bold"] = bold
            fields.append("userEnteredFormat.textFormat.bold")
        if italic is not None:
            text_format["italic"] = italic
            fields.append("userEnteredFormat.textFormat.italic")
        if fontSize is not None:
            text_format["fontSize"] = fontSize
            fields.append("userEnteredFormat.textFormat.fontSize")
        if text_format:
            cell_format["textFormat"] = text_format

        if horizontalAlignment is not None:
            cell_format["horizontalAlignment"] = horizontalAlignment
            fields.append("userEnteredFormat.horizontalAlignment")
        if verticalAlignment is not None:
            cell_format["verticalAlignment"] = verticalAlignment
            fields.append("userEnteredFormat.verticalAlignment")
        if wrapStrategy is not None:
            cell_format["wrapStrategy"] = wrapStrategy
            fields.append("userEnteredFormat.wrapStrategy")

        if borders is not None:
            cell_format["borders"] = {}
            for side, spec in borders.items():
                border_spec = {"style": spec.get("style", "SOLID")}
                if "color" in spec:
                    border_spec["color"] = normalize_color(spec["color"])
                cell_format["borders"][side] = border_spec
            fields.append("userEnteredFormat.borders")

        if not fields:
            response = {"success": True, "updatedRange": range, "message": "No formatting fields provided"}
            logger.info("apply_cell_format response: %s", response)
            return response

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "repeatCell": {
                            "range":  a1_to_grid_range(range, sheet_id),
                            "cell":   {"userEnteredFormat": cell_format},
                            "fields": ",".join(fields),
                        }
                    }
                ]
            },
        ).execute()

        response = {"success": True, "updatedRange": range}
        logger.info("apply_cell_format response: %s", response)
        return response
    except Exception as e:
        logger.error("Error applying cell format: %s", e)
        return {"success": False, "error": str(e)}
