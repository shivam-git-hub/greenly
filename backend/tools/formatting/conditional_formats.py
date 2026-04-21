import logging
from ..utils import a1_to_grid_range, normalize_color, index_to_col_letter

logger = logging.getLogger(__name__)


def add_conditional_format(
    service,
    spreadsheetId: str,
    sheetId: int,
    ranges: list,
    ruleType: str,
    minColor: dict = None,
    midColor: dict = None,
    maxColor: dict = None,
    formula: str = None,
    applyFormat: dict = None,
    priority: int = None,
) -> dict:
    """
    Add a new conditional formatting rule to a sheet.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetId: Numeric sheet ID of the tab to add the rule to.
        ranges: List of ranges the rule applies to. Each element may be either
                an A1 notation string (e.g. 'Sheet1!B5:F13') OR a pre-built
                GridRange dict (e.g. {'startRowIndex': 4, 'endRowIndex': 13,
                'startColumnIndex': 1, 'endColumnIndex': 6}).
                sheetId is injected automatically if omitted from a dict.
        ruleType: 'COLOR_SCALE', 'CUSTOM_FORMULA', or 'TEXT_CONTAINS'.
        minColor: RGB color for minimum value (COLOR_SCALE only).
        midColor: RGB color for midpoint (COLOR_SCALE only). Omit for no midpoint.
        maxColor: RGB color for maximum value (COLOR_SCALE only).
        formula: Boolean formula for CUSTOM_FORMULA rules, e.g. '=A1<0'.
                 Must start with '='.
        applyFormat: CellFormat dict to apply when CUSTOM_FORMULA is true.
        priority: Index to insert at (0 = highest priority). Default: appends
                  at end of the rule list.
    """
    req = {
        "spreadsheetId": spreadsheetId,
        "sheetId": sheetId,
        "ruleType": ruleType,
        "ranges": ranges,
    }
    logger.info("add_conditional_format request: %s", req)

    try:
        def _to_grid_range(r):
            # Accept both A1 strings ("Sheet1!A1:B5") and pre-built GridRange dicts.
            if isinstance(r, dict):
                # Ensure sheetId is set; caller may have omitted it.
                gr = dict(r)
                gr.setdefault("sheetId", sheetId)
                return gr
            return a1_to_grid_range(r, sheetId)

        grid_ranges = [_to_grid_range(r) for r in ranges]

        if ruleType == "COLOR_SCALE":
            gradient_rule = {
                "minpoint": {"color": normalize_color(minColor), "type": "MIN"},
                "maxpoint": {"color": normalize_color(maxColor), "type": "MAX"},
            }
            if midColor:
                gradient_rule["midpoint"] = {
                    "color": normalize_color(midColor),
                    "type": "PERCENTILE",
                    "value": "50",
                }
            rule = {"ranges": grid_ranges, "gradientRule": gradient_rule}
        else:
            cond_type = "CUSTOM_FORMULA" if ruleType == "CUSTOM_FORMULA" else "TEXT_CONTAINS"
            rule = {
                "ranges": grid_ranges,
                "booleanRule": {
                    "condition": {
                        "type": cond_type,
                        "values": [{"userEnteredValue": formula or ""}],
                    },
                    "format": applyFormat or {},
                },
            }

        add_req = {"rule": rule}
        if priority is not None:
            add_req["index"] = priority

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={"requests": [{"addConditionalFormatRule": add_req}]},
        ).execute()

        # The Sheets API does not echo the assigned index in the reply.
        # Read back the live rule list to determine the actual index.
        live = get_conditional_formats(service, spreadsheetId, sheetId)
        if priority is not None:
            rule_index = priority
        else:
            rule_index = len(live.get("rules", [])) - 1  # appended at end

        response = {"success": True, "ruleIndex": rule_index}
        logger.info("add_conditional_format response: %s", response)
        return response
    except Exception as e:
        logger.error("Error adding conditional format: %s", e)
        return {"success": False, "error": str(e)}


def get_conditional_formats(service, spreadsheetId: str, sheetId: int) -> dict:
    """
    Return all conditional formatting rules on a sheet with their live indices.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetId: Numeric sheet ID to list rules for.

    """
    req = {"spreadsheetId": spreadsheetId, "sheetId": sheetId}
    logger.info("get_conditional_formats request: %s", req)

    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            fields="sheets(properties.sheetId,conditionalFormats)",
        ).execute()
    except Exception as e:
        logger.error("Error getting conditional formats: %s", e)
        return {"success": False, "error": str(e)}

    rules = []
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["sheetId"] != sheetId:
            continue
        for idx, rule in enumerate(sheet.get("conditionalFormats", [])):
            rule_ranges = [_grid_range_to_a1(r) for r in rule.get("ranges", [])]
            entry = {"index": idx, "ranges": rule_ranges}

            if "gradientRule" in rule:
                gr = rule["gradientRule"]
                entry["ruleType"]  = "COLOR_SCALE"
                entry["minColor"]  = _api_to_rgb(gr.get("minpoint", {}).get("color", {}))
                entry["maxColor"]  = _api_to_rgb(gr.get("maxpoint", {}).get("color", {}))
                if "midpoint" in gr:
                    entry["midColor"] = _api_to_rgb(gr["midpoint"]["color"])
            elif "booleanRule" in rule:
                br    = rule["booleanRule"]
                ctype = br.get("condition", {}).get("type", "CUSTOM_FORMULA")
                entry["ruleType"]    = ctype
                vals = br.get("condition", {}).get("values", [])
                if vals:
                    entry["condition"] = vals[0].get("userEnteredValue", "")
                entry["applyFormat"] = br.get("format", {})

            rules.append(entry)

    response = {"success": True, "sheetId": sheetId, "rules": rules}
    logger.info("get_conditional_formats response: %d rules", len(rules))
    return response


def delete_conditional_format(
    service, spreadsheetId: str, sheetId: int, ruleIndex: int
) -> dict:
    """
    Delete the conditional format rule at the specified index.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetId: Numeric sheet ID.
        ruleIndex: Current index from a fresh get_conditional_formats call.
                   Never reuse a cached index from a prior call.

    """
    req = {"spreadsheetId": spreadsheetId, "sheetId": sheetId, "ruleIndex": ruleIndex}
    logger.info("delete_conditional_format request: %s", req)

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "deleteConditionalFormatRule": {
                            "sheetId": sheetId,
                            "index":   ruleIndex,
                        }
                    }
                ]
            },
        ).execute()

        remaining = get_conditional_formats(service, spreadsheetId, sheetId)
        response = {
            "success":            True,
            "deletedIndex":       ruleIndex,
            "remainingRuleCount": len(remaining.get("rules", [])),
        }
        logger.info("delete_conditional_format response: %s", response)
        return response
    except Exception as e:
        logger.error("Error deleting conditional format: %s", e)
        return {"success": False, "error": str(e)}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _grid_range_to_a1(gr: dict) -> str:
    sc = gr.get("startColumnIndex", 0)
    ec_val = gr.get("endColumnIndex")
    ec = ec_val - 1 if ec_val is not None else sc

    sr_val = gr.get("startRowIndex")
    sr = sr_val + 1 if sr_val is not None else 1

    er = gr.get("endRowIndex")

    start_col = index_to_col_letter(sc)
    end_col = index_to_col_letter(ec)

    if er is not None:
        return f"{start_col}{sr}:{end_col}{er}"
    else:
        return f"{start_col}{sr}:{end_col}"


def _api_to_rgb(color: dict) -> dict:
    return {
        "r": color.get("red", 0),
        "g": color.get("green", 0),
        "b": color.get("blue", 0),
    }
