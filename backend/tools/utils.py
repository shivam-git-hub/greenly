"""
Shared utilities for the Greenly Sheets tool layer.

Service object
--------------
Every tool function accepts a `service` as its first argument. This is a
Google Sheets API v4 client produced by:

    from tools.utils import build_sheets_service
    service = build_sheets_service(access_token)

`access_token` is the short-lived OAuth 2.0 bearer token that Google Apps
Script returns via ScriptApp.getOAuthToken().  The token is forwarded from
the GAS add-on to this backend in the Authorization header of every request.

The service object wraps all REST calls to the Sheets API and handles HTTP
retries, serialisation, and authentication headers automatically.
"""

import re
import logging
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)


def build_sheets_service(access_token: str):
    """
    Build and return an authenticated Google Sheets API v4 service client.

    Args:
        access_token: Short-lived OAuth 2.0 bearer token from the GAS add-on.
                      Obtained via ScriptApp.getOAuthToken() on the client side.

    Returns:
        googleapiclient Resource: Authenticated Sheets API service. Pass this
        as the first argument (`service`) to every tool function.
    """
    creds = Credentials(token=access_token)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def col_letter_to_index(col: str) -> int:
    """Convert a column letter to a 0-based index. 'A'->0, 'B'->1, 'AA'->26."""
    col = col.upper()
    result = 0
    for c in col:
        result = result * 26 + (ord(c) - ord("A") + 1)
    return result - 1


def index_to_col_letter(idx: int) -> str:
    """Convert a 0-based column index to a letter. 0->'A', 25->'Z', 26->'AA'."""
    result = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(ord("A") + rem) + result
    return result


def parse_a1_range(a1: str) -> dict:
    """
    Parse an A1-notation range string into its components.

    Args:
        a1: A1 notation string, e.g. 'Sheet1!B4:D6' or 'A1:B2'.

    Returns:
        dict with keys: sheet (str|None), startRowIndex (int, 0-based),
        startColumnIndex (int, 0-based), endRowIndex (int, 0-based exclusive),
        endColumnIndex (int, 0-based exclusive).
    """
    if "!" in a1:
        sheet, cell_range = a1.split("!", 1)
        sheet = sheet.strip("'")
    else:
        sheet = None
        cell_range = a1

    if ":" in cell_range:
        start, end = cell_range.split(":", 1)
    else:
        start = end = cell_range

    def parse_cell(cell):
        # Full reference like B4
        m = re.match(r"^([A-Za-z]+)(\d+)$", cell)
        if m:
            return int(m.group(2)) - 1, col_letter_to_index(m.group(1))
        # Column-only like A or AA
        m = re.match(r"^([A-Za-z]+)$", cell)
        if m:
            return None, col_letter_to_index(m.group(1))
        # Row-only like 1 or 10
        m = re.match(r"^(\d+)$", cell)
        if m:
            return int(m.group(1)) - 1, None
        return None, None

    sr, sc = parse_cell(start)
    er, ec = parse_cell(end)
    return {
        "sheet": sheet,
        "startRowIndex": sr,
        "startColumnIndex": sc,
        "endRowIndex": er + 1 if er is not None else None,
        "endColumnIndex": ec + 1 if ec is not None else None,
    }


def a1_to_grid_range(a1: str, sheet_id: int) -> dict:
    """
    Convert an A1-notation range to a Sheets API GridRange object.

    Args:
        a1: A1 notation string, e.g. 'Sheet1!B4:D6'.
        sheet_id: Numeric sheetId for the GridRange.

    Returns:
        dict: GridRange with sheetId, startRowIndex, endRowIndex,
              startColumnIndex, endColumnIndex (all 0-based).
    """
    p = parse_a1_range(a1)
    gr = {"sheetId": sheet_id}
    if p["startColumnIndex"] is not None:
        gr["startColumnIndex"] = p["startColumnIndex"]
        gr["endColumnIndex"] = p["endColumnIndex"]
    if p["startRowIndex"] is not None:
        gr["startRowIndex"] = p["startRowIndex"]
        gr["endRowIndex"] = p["endRowIndex"]
    return gr


def get_sheet_id_by_name(service, spreadsheet_id: str, sheet_name: str) -> int:
    """
    Look up the numeric sheetId for a tab by its display name.

    Args:
        service: Authenticated Sheets API service (from build_sheets_service).
        spreadsheet_id: The spreadsheet ID.
        sheet_name: Tab display name, e.g. 'Assumptions'.

    Returns:
        int: Numeric sheetId used in batchUpdate requests.

    Raises:
        ValueError: If no sheet with that name exists.
    """
    result = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    for sheet in result.get("sheets", []):
        if sheet["properties"]["title"] == sheet_name:
            return sheet["properties"]["sheetId"]
    raise ValueError(f"Sheet '{sheet_name}' not found in spreadsheet '{spreadsheet_id}'")


def normalize_color(color) -> dict:
    """
    Normalise a colour dict (or string) to the Sheets API format {red, green, blue}.

    Accepts:
    - Dict with r/g/b or red/green/blue keys, values 0.0-1.0
    - Color name strings: "green", "red", "blue", "white", "black", "yellow", etc.
    - Hex strings: "#00FF00", "#0F0", "00FF00"

    Args:
        color: Dict or string with color value.

    Returns:
        dict: {red, green, blue} as expected by the Sheets API Color type.
    """
    if not color:
        return {}

    # Handle string colors (name or hex)
    if isinstance(color, str):
        color = color.strip().lower()
        # Common color names to RGB (0.0-1.0 range)
        color_map = {
            "black":  {"r": 0.0, "g": 0.0, "b": 0.0},
            "white": {"r": 1.0, "g": 1.0, "b": 1.0},
            "red":   {"r": 1.0, "g": 0.0, "b": 0.0},
            "green": {"r": 0.0, "g": 0.8, "b": 0.0},
            "blue":  {"r": 0.0, "g": 0.0, "b": 1.0},
            "yellow": {"r": 1.0, "g": 1.0, "b": 0.0},
            "cyan": {"r": 0.0, "g": 1.0, "b": 1.0},
            "magenta": {"r": 1.0, "g": 0.0, "b": 1.0},
            "orange": {"r": 1.0, "g": 0.647, "b": 0.0},
            "purple": {"r": 0.58, "g": 0.0, "b": 0.58},
            "pink": {"r": 1.0, "g": 0.753, "b": 0.796},
            "gray": {"r": 0.5, "g": 0.5, "b": 0.5},
            "grey": {"r": 0.5, "g": 0.5, "b": 0.5},
            "dark green": {"r": 0.118, "g": 0.306, "b": 0.169},
            "light green": {"r": 0.565, "g": 0.933, "b": 0.565},
            "navy": {"r": 0.0, "g": 0.0, "b": 0.502},
            "teal": {"r": 0.0, "g": 0.502, "b": 0.502},
            "maroon": {"r": 0.502, "g": 0.0, "b": 0.0},
            "olive": {"r": 0.502, "g": 0.502, "b": 0.0},
            "silver": {"r": 0.753, "g": 0.753, "b": 0.753},
            "lime": {"r": 0.196, "g": 0.804, "b": 0.196},
            "aqua": {"r": 0.0, "g": 1.0, "b": 1.0},
            "fuchsia": {"r": 1.0, "g": 0.0, "b": 1.0},
        }
        if color in color_map:
            return color_map[color]

        # Try hex color (with or without #)
        hex_match = color.lstrip("#")
        if len(hex_match) in (3, 6, 8):
            try:
                if len(hex_match) == 3:
                    # Short form: #0F0 -> #00FF00
                    hex_match = "".join(c * 2 for c in hex_match)
                # Parse full hex
                num = int(hex_match, 16)
                r = ((num >> 16) & 0xFF) / 255.0
                g = ((num >> 8) & 0xFF) / 255.0
                b = (num & 0xFF) / 255.0
                return {"red": r, "green": g, "blue": b}
            except (ValueError, TypeError):
                pass

        # Unknown string - return safe default
        return {"red": 0, "green": 0, "blue": 0}

    # Handle dict with r/g/b or red/green/blue
    return {
        "red":   color.get("red",   color.get("r", 0)),
        "green": color.get("green", color.get("g", 0)),
        "blue":  color.get("blue",  color.get("b", 0)),
    }


def get_effective_value(cell_data: dict):
    """
    Extract the computed Python value from a Sheets API CellData object.

    Returns the effectiveValue (post-formula evaluation), not the raw
    userEnteredValue, so callers always see the actual cell result.

    Args:
        cell_data: A CellData dict from the Sheets API gridData response.

    Returns:
        The cell value as a Python int/float, str, bool, or None if empty.
        Error cells (e.g. #REF!) are returned as the error string.
    """
    ev = cell_data.get("effectiveValue", {})
    if "numberValue" in ev:
        return ev["numberValue"]
    if "stringValue" in ev:
        return ev["stringValue"]
    if "boolValue" in ev:
        return ev["boolValue"]
    if "errorValue" in ev:
        return f"#{ev['errorValue'].get('type', 'ERROR')}!"
    return None


def get_formula(cell_data: dict):
    """
    Extract the formula string from a Sheets API CellData object.

    Args:
        cell_data: A CellData dict from the Sheets API gridData response.

    Returns:
        str: Formula string starting with '=', or None if the cell has no formula.
    """
    uev = cell_data.get("userEnteredValue", {})
    return uev.get("formulaValue")
