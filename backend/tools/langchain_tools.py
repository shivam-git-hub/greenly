import json
from typing import List, Optional
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .cell_content.write_values import write_values
from .cell_content.validate_formula import validate_formula
from .cell_content.write_formulas import write_formulas
from .read_structure.read_range import read_range
from .read_structure.read_sheet_structure import read_sheet_structure
from .read_structure.get_chunk import get_chunk
from .formatting.apply_cell_format import apply_cell_format
from .formatting.clear_cell_format import clear_cell_format
from .formatting.get_cell_format import get_cell_format
from .formatting.conditional_formats import (
    add_conditional_format,
    get_conditional_formats,
    delete_conditional_format,
)
from .charts.charts import create_chart, get_charts, delete_chart
from .pivot_tables.pivot_tables import create_pivot_table, get_pivot_tables, delete_pivot_table
from .named_ranges.named_ranges import create_named_range, get_named_ranges, delete_named_range
from .data_validation.data_validation import (
    set_data_validation,
    get_data_validations,
    clear_data_validation,
)
from .formula_utils.audit_formulas import audit_formulas
from .formula_utils.trace_dependents import trace_dependents
from .sheet_structure.create_sheet import create_sheet
from .common_tools.common_tools import (
    web_search,
    run_python,
    load_sheet_to_df,
    write_df_to_sheet,
    _make_sandbox_namespace,
)


# ── Input schemas ──────────────────────────────────────────────────────────────

class WriteValuesInput(BaseModel):
    spreadsheetId: str
    range: str
    values: list
    valueInputOption: str = "USER_ENTERED"

class ValidateFormulaInput(BaseModel):
    formula: str
    targetCell: str
    spreadsheetId: str

class WriteFormulasInput(BaseModel):
    spreadsheetId: str
    range: str
    formulas: list
    validateFirst: bool = True
    abortOnError: bool = True

class ReadRangeInput(BaseModel):
    spreadsheetId: str
    range: str

class ReadSheetStructureInput(BaseModel):
    spreadsheetId: str

class GetChunkInput(BaseModel):
    spreadsheetId: str
    sheetTitle: str
    startRow: int
    endRow: int
    columns: str = None

class ApplyCellFormatInput(BaseModel):
    spreadsheetId: str
    range: str
    numberFormat: dict = None
    backgroundColor: dict = None
    textColor: dict = None
    bold: bool = None
    italic: bool = None
    fontSize: int = None
    horizontalAlignment: str = None
    verticalAlignment: str = None
    wrapStrategy: str = None
    borders: dict = None

class ClearCellFormatInput(BaseModel):
    spreadsheetId: str
    range: str
    fields: str = None

class GetCellFormatInput(BaseModel):
    spreadsheetId: str
    range: str

class AddConditionalFormatInput(BaseModel):
    spreadsheetId: str
    sheetId: int
    ranges: list
    ruleType: str
    minColor: dict = None
    midColor: dict = None
    maxColor: dict = None
    formula: str = None
    applyFormat: dict = None
    priority: int = None

class GetConditionalFormatsInput(BaseModel):
    spreadsheetId: str
    sheetId: int

class DeleteConditionalFormatInput(BaseModel):
    spreadsheetId: str
    sheetId: int
    ruleIndex: int

class CreateChartInput(BaseModel):
    spreadsheetId: str
    sheetId: int
    chartType: str
    dataRanges: list
    title: str = None
    anchorRow: int = 0
    anchorCol: int = 0
    offsetX: int = 0
    offsetY: int = 0
    width: int = 600
    height: int = 371
    xAxisLabel: str = None
    yAxisLabel: str = None
    legendPosition: str = "BOTTOM_LEGEND"
    seriesColors: list = None
    headerCount: int = 1

class GetChartsInput(BaseModel):
    spreadsheetId: str
    sheetId: int

class DeleteChartInput(BaseModel):
    spreadsheetId: str
    chartId: int

class CreatePivotTableInput(BaseModel):
    spreadsheetId: str
    sourceRange: str
    anchorCell: str
    rows: list
    values: list
    columns: list = None
    filters: list = None

class GetPivotTablesInput(BaseModel):
    spreadsheetId: str
    sheetTitle: str

class DeletePivotTableInput(BaseModel):
    spreadsheetId: str
    anchorCell: str
    clearOutput: bool = True

class CreateNamedRangeInput(BaseModel):
    spreadsheetId: str
    name: str
    range: str

class GetNamedRangesInput(BaseModel):
    spreadsheetId: str

class DeleteNamedRangeInput(BaseModel):
    spreadsheetId: str
    namedRangeId: str

class SetDataValidationInput(BaseModel):
    spreadsheetId: str
    range: str
    conditionType: str
    values: list = None
    formula: str = None
    showDropdown: bool = True
    strict: bool = True
    inputMessage: str = None
    errorMessage: str = None

class GetDataValidationsInput(BaseModel):
    spreadsheetId: str
    sheetTitle: str

class ClearDataValidationInput(BaseModel):
    spreadsheetId: str
    range: str

class AuditFormulasInput(BaseModel):
    spreadsheetId: str
    range: str

class TraceDependentsInput(BaseModel):
    spreadsheetId: str
    cell: str
    direction: str = "both"
    maxDepth: int = 3

class CreateSheetInput(BaseModel):
    spreadsheetId: str
    title: str
    index: int = None
    tabColor: dict = None
    gridProperties: dict = None

class WebSearchInput(BaseModel):
    query: str = Field(..., description="Search query string")
    num_results: int = Field(5, description="Maximum number of results to return")

class RunPythonInput(BaseModel):
    code: str = Field(..., description="Python code to execute in the sandbox")
    timeout: int = Field(30, description="Maximum execution time in seconds")

class LoadSheetToDfInput(BaseModel):
    spreadsheetId: str = Field(..., description="Google Spreadsheet ID")
    sheetTitle: str = Field(..., description="Tab (sheet) name to load")
    columns: Optional[List[str]] = Field(None, description="Column names to keep. Omit to load all columns.")
    varName: str = Field("df", description="Variable name for the DataFrame in the sandbox")

class WriteDfToSheetInput(BaseModel):
    spreadsheetId: str = Field(..., description="Google Spreadsheet ID")
    range: str = Field(..., description="A1-notation start cell, e.g. 'Sheet1!A1'")
    varName: str = Field("df", description="Sandbox variable name of the DataFrame to write")
    includeHeader: bool = Field(True, description="Write column names as the first row")


# ── Shared helper ──────────────────────────────────────────────────────────────

def _wrap(fn, schema, service):
    def runner(**kwargs):
        return json.dumps(fn(service, **kwargs), default=str)
    return StructuredTool.from_function(
        func=runner,
        name=fn.__name__,
        description=fn.__doc__ or fn.__name__,
        args_schema=schema,
    )


# ── Factories ──────────────────────────────────────────────────────────────────

def create_read_tools(service) -> list:
    """Tools that only read or inspect spreadsheet data — no mutations."""
    return [
        _wrap(read_range, ReadRangeInput, service),
        _wrap(read_sheet_structure, ReadSheetStructureInput, service),
        _wrap(get_chunk, GetChunkInput, service),
        _wrap(get_cell_format, GetCellFormatInput, service),
        _wrap(get_conditional_formats, GetConditionalFormatsInput, service),
        _wrap(get_charts, GetChartsInput, service),
        _wrap(get_pivot_tables, GetPivotTablesInput, service),
        _wrap(get_named_ranges, GetNamedRangesInput, service),
        _wrap(get_data_validations, GetDataValidationsInput, service),
        _wrap(validate_formula, ValidateFormulaInput, service),
        _wrap(audit_formulas, AuditFormulasInput, service),
        _wrap(trace_dependents, TraceDependentsInput, service),
    ]


def create_write_tools(service) -> list:
    """Tools that mutate spreadsheet data, structure, or formatting."""
    return [
        _wrap(write_values, WriteValuesInput, service),
        _wrap(write_formulas, WriteFormulasInput, service),
        _wrap(apply_cell_format, ApplyCellFormatInput, service),
        _wrap(clear_cell_format, ClearCellFormatInput, service),
        _wrap(add_conditional_format, AddConditionalFormatInput, service),
        _wrap(delete_conditional_format, DeleteConditionalFormatInput, service),
        _wrap(create_chart, CreateChartInput, service),
        _wrap(delete_chart, DeleteChartInput, service),
        _wrap(create_pivot_table, CreatePivotTableInput, service),
        _wrap(delete_pivot_table, DeletePivotTableInput, service),
        _wrap(create_named_range, CreateNamedRangeInput, service),
        _wrap(delete_named_range, DeleteNamedRangeInput, service),
        _wrap(set_data_validation, SetDataValidationInput, service),
        _wrap(clear_data_validation, ClearDataValidationInput, service),
        _wrap(create_sheet, CreateSheetInput, service),
    ]


def create_python_tools(service) -> list:
    """Python sandbox + Sheet ↔ DataFrame I/O tools sharing one sandbox namespace."""
    ns = _make_sandbox_namespace()

    def _run_python(code: str, timeout: int = 30):
        return json.dumps(run_python(code, timeout, _ns=ns), default=str)

    def _load_sheet_to_df(
        spreadsheetId: str,
        sheetTitle: str,
        columns: Optional[List[str]] = None,
        varName: str = "df",
    ):
        return json.dumps(
            load_sheet_to_df(service, spreadsheetId, sheetTitle, columns, varName, _ns=ns),
            default=str,
        )

    def _write_df_to_sheet(
        spreadsheetId: str,
        range: str,
        varName: str = "df",
        includeHeader: bool = True,
    ):
        return json.dumps(
            write_df_to_sheet(service, spreadsheetId, range, varName, includeHeader, _ns=ns),
            default=str,
        )

    return [
        StructuredTool.from_function(
            func=_run_python,
            name=run_python.__name__,
            description=run_python.__doc__ or run_python.__name__,
            args_schema=RunPythonInput,
        ),
        StructuredTool.from_function(
            func=_load_sheet_to_df,
            name=load_sheet_to_df.__name__,
            description=load_sheet_to_df.__doc__ or load_sheet_to_df.__name__,
            args_schema=LoadSheetToDfInput,
        ),
        StructuredTool.from_function(
            func=_write_df_to_sheet,
            name=write_df_to_sheet.__name__,
            description=write_df_to_sheet.__doc__ or write_df_to_sheet.__name__,
            args_schema=WriteDfToSheetInput,
        ),
    ]


def create_research_tools() -> list:
    """Web search tool. No Sheets service required."""

    def _web_search(query: str, num_results: int = 5):
        return json.dumps(web_search(query, num_results), default=str)

    return [
        StructuredTool.from_function(
            func=_web_search,
            name=web_search.__name__,
            description=web_search.__doc__ or web_search.__name__,
            args_schema=WebSearchInput,
        ),
    ]
