import re
import logging
from ..utils import get_formula, get_effective_value, index_to_col_letter

logger = logging.getLogger(__name__)

_ERROR_MAP = {
    "REF":      "REF_ERROR",
    "NAME":     "NAME_ERROR",
    "DIV_ZERO": "DIV_ZERO",
    "DIV/0":    "DIV_ZERO",
    "PARSE":    "FORMULA_PARSE",
    "CIRCULAR": "CIRCULAR",
    "NA":       "NAME_ERROR",
    "VALUE":    "FORMULA_PARSE",
    "NUM":      "FORMULA_PARSE",
    "NULL":     "FORMULA_PARSE",
}
_ERROR_VALUE_RE = re.compile(r"#([A-Z_/0-9]+)!")
_VOLATILE_RE    = re.compile(r"\b(NOW|TODAY|RAND|RANDBETWEEN|INDIRECT|OFFSET)\b", re.I)


def audit_formulas(service, spreadsheetId: str, range: str) -> dict:
    """
    Scan a cell range for formula errors, broken references, and warnings.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        range: A1 notation range to audit, e.g. 'Model!A1:L80'. Use the full
               tab range for a complete audit.
    """
    req = {"spreadsheetId": spreadsheetId, "range": range}
    logger.info("audit_formulas request: %s", req)

    try:
        result = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            ranges=[range],
            includeGridData=True,
            fields="sheets.data(startRow,startColumn,rowData.values(userEnteredValue,effectiveValue))",
        ).execute()

        sheet_prefix = range.split("!")[0] + "!" if "!" in range else ""

        errors   = []
        warnings = []

        # Track formula presence per absolute column for hardcoded-value detection
        col_has_formula: dict[int, list] = {}

        for sheet in result.get("sheets", []):
            for grid in sheet.get("data", []):
                start_row_offset = grid.get("startRow", 0)
                start_col_offset = grid.get("startColumn", 0)
                all_rows = grid.get("rowData", [])

                for ri, row_data in enumerate(all_rows):
                    cells = row_data.get("values", [])
                    for ci, cell in enumerate(cells):
                        abs_row = start_row_offset + ri
                        abs_col = start_col_offset + ci
                        cell_a1 = f"{sheet_prefix}{index_to_col_letter(abs_col)}{abs_row + 1}"

                        value   = get_effective_value(cell)
                        formula = get_formula(cell)

                        # Detect API-reported error values
                        if isinstance(value, str):
                            m = _ERROR_VALUE_RE.match(value)
                            if m:
                                err_raw  = m.group(1).replace("/", "_")
                                err_type = _ERROR_MAP.get(err_raw, "REF_ERROR")
                                errors.append({
                                    "cell":   cell_a1,
                                    "type":   err_type,
                                    "detail": f"Cell evaluates to {value}",
                                })
                                continue

                        if formula:
                            col_has_formula.setdefault(abs_col, []).append((abs_row, formula))
                            # Flag volatile functions
                            if _VOLATILE_RE.search(formula):
                                warnings.append({
                                    "cell":   cell_a1,
                                    "type":   "VOLATILE_FUNCTION",
                                    "detail": f"Formula contains a volatile function: {formula}",
                                })
                        else:
                            uev = cell.get("userEnteredValue", {})
                            if "numberValue" in uev:
                                col_has_formula.setdefault(abs_col, []).append((abs_row, None))

        # Warn on hardcoded numerics in columns that also contain formulas
        for abs_col, entries in col_has_formula.items():
            has_formulas    = any(e[1] is not None for e in entries)
            non_formula_rows = [e for e in entries if e[1] is None]
            if has_formulas and non_formula_rows:
                for abs_row, _ in non_formula_rows:
                    cell_a1 = f"{sheet_prefix}{index_to_col_letter(abs_col)}{abs_row + 1}"
                    warnings.append({
                        "cell":   cell_a1,
                        "type":   "HARDCODED_VALUE",
                        "detail": "Cell contains a literal value where neighbouring cells in the same column use formulas",
                    })

        response = {
            "success":      True,
            "audited":      range,
            "errorCount":   len(errors),
            "warningCount": len(warnings),
            "errors":       errors,
            "warnings":     warnings,
        }
        logger.info(
            "audit_formulas response: %d errors, %d warnings", len(errors), len(warnings)
        )
        return response
    except Exception as e:
        logger.error("Error auditing formulas: %s", e)
        return {"success": False, "error": str(e)}
