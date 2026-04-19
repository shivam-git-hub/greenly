import logging
from ..utils import parse_a1_range, get_sheet_id_by_name, index_to_col_letter

logger = logging.getLogger(__name__)


def create_pivot_table(
    service,
    spreadsheetId: str,
    sourceRange: str,
    anchorCell: str,
    rows: list,
    values: list,
    columns: list = None,
    filters: list = None,
) -> dict:
    """
    Create a live pivot table anchored at a destination cell.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sourceRange: A1 notation of source data including headers,
                     e.g. 'RawData!A1:F500'.
        anchorCell: A1 notation of where pivot output starts,
                    e.g. 'Dashboard!A2'. This becomes the identifier.
        rows: List of {columnIndex (0-based), sortOrder: ASCENDING|DESCENDING}
              for row groupings.
        values: List of {columnIndex, summarizeFunction: SUM|COUNT|AVERAGE|MAX|MIN}
                for value aggregations.
        columns: List of {columnIndex, sortOrder} for column groupings.
        filters: List of {columnIndex, condition: {type, values}} filter specs.
    """
    req = {
        "spreadsheetId": spreadsheetId,
        "sourceRange": sourceRange,
        "anchorCell": anchorCell,
    }
    logger.info("create_pivot_table request: %s", req)

    try:
        parsed_anchor = parse_a1_range(anchorCell)
        anchor_sheet_id = get_sheet_id_by_name(service, spreadsheetId, parsed_anchor["sheet"])

        parsed_source = parse_a1_range(sourceRange)
        source_sheet_id = get_sheet_id_by_name(service, spreadsheetId, parsed_source["sheet"])

        source_grid_range = {
            "sheetId":          source_sheet_id,
            "startRowIndex":    parsed_source["startRowIndex"],
            "endRowIndex":      parsed_source["endRowIndex"],
            "startColumnIndex": parsed_source["startColumnIndex"],
            "endColumnIndex":   parsed_source["endColumnIndex"],
        }
        source_grid_range = {k: v for k, v in source_grid_range.items() if v is not None}

        criteria = {
            str(f["columnIndex"]): {
                "condition": {
                    "type":   f["condition"]["type"],
                    "values": [{"userEnteredValue": str(v)} for v in f["condition"].get("values", [])],
                }
            }
            for f in (filters or [])
        }

        pivot_table = {
            "source":   source_grid_range,
            "rows":     [{"sourceColumnOffset": r["columnIndex"], "sortOrder": r.get("sortOrder", "ASCENDING")} for r in rows],
            "columns":  [{"sourceColumnOffset": c["columnIndex"], "sortOrder": c.get("sortOrder", "ASCENDING")} for c in (columns or [])],
            "values":   [{"sourceColumnOffset": v["columnIndex"], "summarizeFunction": v.get("summarizeFunction", "SUM")} for v in values],
            "criteria": criteria,
        }

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "updateCells": {
                            "rows":   [{"values": [{"pivotTable": pivot_table}]}],
                            "fields": "pivotTable",
                            "start": {
                                "sheetId":     anchor_sheet_id,
                                "rowIndex":    parsed_anchor["startRowIndex"],
                                "columnIndex": parsed_anchor["startColumnIndex"],
                            },
                        }
                    }
                ]
            },
        ).execute()

        response = {"success": True, "anchorCell": anchorCell}
        logger.info("create_pivot_table response: %s", response)
        return response
    except Exception as e:
        logger.error("Error creating pivot table: %s", e)
        return {"success": False, "error": str(e)}


def get_pivot_tables(service, spreadsheetId: str, sheetTitle: str) -> dict:
    """
    List all pivot tables on a sheet with anchor cells and configuration.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetTitle: Tab name to scan for pivot tables, e.g. 'Dashboard'.

    """
    req = {"spreadsheetId": spreadsheetId, "sheetTitle": sheetTitle}
    logger.info("get_pivot_tables request: %s", req)

    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            includeGridData=True,
            fields="sheets(properties(title,sheetId),data(startRow,startColumn,rowData.values.pivotTable))",
        ).execute()

        sheets_map = {
            s["properties"]["sheetId"]: s["properties"]["title"]
            for s in meta.get("sheets", [])
        }

        pivot_tables = []
        for sheet in meta.get("sheets", []):
            if sheet["properties"]["title"] != sheetTitle:
                continue
            for grid in sheet.get("data", []):
                start_row_offset = grid.get("startRow", 0)
                start_col_offset = grid.get("startColumn", 0)
                for ri, row_data in enumerate(grid.get("rowData", [])):
                    for ci, cell in enumerate(row_data.get("values", [])):
                        pt = cell.get("pivotTable")
                        if not pt:
                            continue
                        
                        abs_row = start_row_offset + ri
                        abs_col = start_col_offset + ci
                        safe_anchor_sheet = f"'{sheetTitle.replace(chr(39), chr(39)+chr(39))}'" if " " in sheetTitle or not sheetTitle.isalnum() else sheetTitle
                        anchor = f"{safe_anchor_sheet}!{index_to_col_letter(abs_col)}{abs_row + 1}"
                        
                        src    = pt.get("source", {})
                        src_name = sheets_map.get(src.get("sheetId", 0), str(src.get("sheetId", 0)))
                        safe_src_name = f"'{src_name.replace(chr(39), chr(39)+chr(39))}'" if " " in src_name or not src_name.isalnum() else src_name
                        
                        sc = src.get("startColumnIndex", 0)
                        ec_val = src.get("endColumnIndex")
                        ec = ec_val - 1 if ec_val is not None else sc
                        
                        sr_val = src.get("startRowIndex")
                        sr = sr_val + 1 if sr_val is not None else 1
                        
                        er = src.get("endRowIndex")
                        
                        if er is not None:
                            src_range = f"{safe_src_name}!{index_to_col_letter(sc)}{sr}:{index_to_col_letter(ec)}{er}"
                        else:
                            src_range = f"{safe_src_name}!{index_to_col_letter(sc)}{sr}:{index_to_col_letter(ec)}"
                            
                        pivot_tables.append({
                            "anchorCell":   anchor,
                            "sourceRange":  src_range,
                            "rowFields":    [str(r.get("sourceColumnOffset", "")) for r in pt.get("rows", [])],
                            "columnFields": [str(c.get("sourceColumnOffset", "")) for c in pt.get("columns", [])],
                            "valueFields":  [
                                {
                                    "columnName":        str(v.get("sourceColumnOffset", "")),
                                    "summarizeFunction": v.get("summarizeFunction", "SUM"),
                                }
                                for v in pt.get("values", [])
                            ],
                        })

        response = {"success": True, "sheetTitle": sheetTitle, "pivotTables": pivot_tables}
        logger.info("get_pivot_tables response: %d pivot tables", len(pivot_tables))
        return response
    except Exception as e:
        logger.error("Error getting pivot tables: %s", e)
        return {"success": False, "error": str(e)}


def delete_pivot_table(
    service, spreadsheetId: str, anchorCell: str, clearOutput: bool = True
) -> dict:
    """
    Delete the pivot table at the specified anchor cell.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        anchorCell: A1 notation of the pivot table anchor cell from
                    get_pivot_tables, e.g. 'Dashboard!A2'.
        clearOutput: If True (default), also clears cells in the pivot output
                     area. If False, leaves computed values as static data.
    """
    req = {"spreadsheetId": spreadsheetId, "anchorCell": anchorCell, "clearOutput": clearOutput}
    logger.info("delete_pivot_table request: %s", req)

    try:
        parsed  = parse_a1_range(anchorCell)
        sheet_id = get_sheet_id_by_name(service, spreadsheetId, parsed["sheet"])

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "updateCells": {
                            "rows":   [{"values": [{"pivotTable": {}}]}],
                            "fields": "pivotTable",
                            "start": {
                                "sheetId":     sheet_id,
                                "rowIndex":    parsed["startRowIndex"],
                                "columnIndex": parsed["startColumnIndex"],
                            },
                        }
                    }
                ]
            },
        ).execute()

        response = {"success": True, "anchorCell": anchorCell}
        logger.info("delete_pivot_table response: %s", response)
        return response
    except Exception as e:
        logger.error("Error deleting pivot table: %s", e)
        return {"success": False, "error": str(e)}
