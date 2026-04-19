import logging
from ..utils import get_effective_value

logger = logging.getLogger(__name__)

MAX_ROWS_PER_CHUNK = 2000


def get_chunk(
    service,
    spreadsheetId: str,
    sheetTitle: str,
    startRow: int,
    endRow: int,
    columns: str = None,
) -> dict:
    """
    Read a row-bounded slice of a large sheet for paginated access.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetTitle: The tab name to read from, e.g. 'RawData'.
        startRow: 1-based row index to start reading from.
        endRow: 1-based row index to stop at (inclusive). Clamped to
                startRow + 1999.
        columns: Optional column range to include, e.g. 'A:F'. Defaults to
                 all columns in the used range.
    """
    try:
        clamped_end = min(endRow, startRow + MAX_ROWS_PER_CHUNK - 1)
        safe_sheet_title = f"'{sheetTitle.replace(chr(39), chr(39)+chr(39))}'" if " " in sheetTitle or not sheetTitle.isalnum() else sheetTitle

        if columns:
            col_start, col_end = (columns.split(":") + [columns])[:2]
            range_str = f"{safe_sheet_title}!{col_start}{startRow}:{col_end}{clamped_end}"
        else:
            range_str = f"{safe_sheet_title}!{startRow}:{clamped_end}"

        req = {
            "spreadsheetId": spreadsheetId,
            "sheetTitle": sheetTitle,
            "startRow": startRow,
            "endRow": clamped_end,
            "columns": columns,
        }
        logger.info("get_chunk request: %s", req)

        result = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            ranges=[range_str],
            includeGridData=True,
            fields="sheets.data.rowData.values.effectiveValue",
        ).execute()

        values = []
        for sheet_data in result.get("sheets", []):
            for grid_data in sheet_data.get("data", []):
                for row_data in grid_data.get("rowData", []):
                    values.append([
                        get_effective_value(c)
                        for c in row_data.get("values", [])
                    ])

        has_more = False
        if clamped_end < endRow:
            # Determine whether more rows exist by checking the sheet's total row count
            meta = service.spreadsheets().get(
                spreadsheetId=spreadsheetId,
                fields="sheets(properties(title,gridProperties.rowCount))",
            ).execute()
            sheet_row_count = 0
            for s in meta.get("sheets", []):
                if s["properties"]["title"] == sheetTitle:
                    sheet_row_count = s["properties"]["gridProperties"].get("rowCount", 0)
                    break
            has_more = clamped_end < sheet_row_count

        response = {
            "success":      True,
            "range":        range_str,
            "rowCount":     len(values),
            "hasMore":      has_more,
            "nextStartRow": clamped_end + 1 if has_more else None,
            "values":       values,
        }
        logger.info(
            "get_chunk response: range=%s rows=%d hasMore=%s",
            range_str, len(values), has_more,
        )
        return response
    except Exception as e:
        logger.error("Error getting chunk: %s", e)
        return {"success": False, "error": str(e)}
