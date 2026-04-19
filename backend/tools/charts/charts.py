import logging
from ..utils import a1_to_grid_range, normalize_color, index_to_col_letter

logger = logging.getLogger(__name__)


def create_chart(
    service,
    spreadsheetId: str,
    sheetId: int,
    chartType: str,
    dataRanges: list,
    title: str = None,
    anchorRow: int = 0,
    anchorCol: int = 0,
    offsetX: int = 0,
    offsetY: int = 0,
    width: int = 600,
    height: int = 371,
    xAxisLabel: str = None,
    yAxisLabel: str = None,
    legendPosition: str = "BOTTOM_LEGEND",
    seriesColors: list = None,
    headerCount: int = 1,
) -> dict:
    """
    Create an embedded chart on a sheet.

    Charts are EmbeddedChart objects positioned by anchor cell and pixel offset. One chart can visualise multiple data ranges.

    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetId: Numeric sheet ID to embed the chart on.
        chartType: Chart type string: BAR, COLUMN, LINE, AREA, PIE, SCATTER,
                   COMBO, WATERFALL, CANDLESTICK, or STEPPED_AREA.
        dataRanges: List of A1 notation ranges. The first range is used as the
                    domain (x-axis); subsequent ranges become series.
        title: Chart title displayed above the chart.
        anchorRow: 0-based row index of the top-left anchor cell. Default: 0.
        anchorCol: 0-based column index of the top-left anchor cell. Default: 0.
        offsetX: Pixel offset from the left edge of the anchor cell. Default: 0.
        offsetY: Pixel offset from the top edge of the anchor cell. Default: 0.
        width: Chart width in pixels. Default: 600.
        height: Chart height in pixels. Default: 371.
        xAxisLabel: Label for the horizontal axis.
        yAxisLabel: Label for the vertical axis.
        legendPosition: BOTTOM_LEGEND, RIGHT_LEGEND, LEFT_LEGEND, TOP_LEGEND,
                        or NO_LEGEND.
        seriesColors: List of RGB dicts to override default series colours.
        headerCount: Number of header rows in the data range. Default: 1.
    """
    req = {
        "spreadsheetId": spreadsheetId,
        "sheetId": sheetId,
        "chartType": chartType,
        "dataRanges": dataRanges,
        "title": title,
    }
    logger.info("create_chart request: %s", req)

    data_source_ranges = [
        {"sources": [a1_to_grid_range(r, sheetId)]} for r in dataRanges
    ]

    axis = []
    if xAxisLabel:
        axis.append({"position": "BOTTOM_AXIS", "title": xAxisLabel})
    if yAxisLabel:
        axis.append({"position": "LEFT_AXIS", "title": yAxisLabel})

    series = []
    src_list = data_source_ranges[1:] if len(data_source_ranges) > 1 else data_source_ranges
    for i, dsr in enumerate(src_list):
        s = {"series": {"sourceRange": dsr}, "targetAxis": "LEFT_AXIS"}
        if seriesColors and i < len(seriesColors):
            s["colorStyle"] = {"rgbColor": normalize_color(seriesColors[i])}
        series.append(s)
    if not series:
        series = [{"series": {"sourceRange": data_source_ranges[0]}, "targetAxis": "LEFT_AXIS"}]

    if chartType == "PIE":
        pie_series = {"sourceRange": data_source_ranges[1]} if len(data_source_ranges) > 1 else {"sourceRange": data_source_ranges[0]}
        spec = {
            "pieChart": {
                "legendPosition": legendPosition,
                "domain": {"sourceRange": data_source_ranges[0]},
                "series": pie_series,
            }
        }
    elif chartType == "WATERFALL":
        waterfall_series = [{"data": {"sourceRange": dsr}} for dsr in src_list]
        if not waterfall_series:
            waterfall_series = [{"data": {"sourceRange": data_source_ranges[0]}}]
        spec = {
            "waterfallChart": {
                "domain": {"sourceRange": data_source_ranges[0]},
                "series": waterfall_series,
            }
        }
    elif chartType == "CANDLESTICK":
        candlestick_data = {
            "lowSeries": {"data": {"sourceRange": data_source_ranges[1]}} if len(data_source_ranges) > 1 else {},
            "openSeries": {"data": {"sourceRange": data_source_ranges[2]}} if len(data_source_ranges) > 2 else {},
            "closeSeries": {"data": {"sourceRange": data_source_ranges[3]}} if len(data_source_ranges) > 3 else {},
            "highSeries": {"data": {"sourceRange": data_source_ranges[4]}} if len(data_source_ranges) > 4 else {}
        }
        spec = {
            "candlestickChart": {
                "domain": {"data": {"sourceRange": data_source_ranges[0]}},
                "data": [candlestick_data]
            }
        }
    else:
        spec = {
            "basicChart": {
                "chartType":     chartType,
                "legendPosition": legendPosition,
                "axis":          axis,
                "domains":       [{"domain": {"sourceRange": data_source_ranges[0]}}],
                "series":        series,
                "headerCount":   headerCount,
            }
        }
    if title:
        spec["title"] = title

    try:
        result = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={
                "requests": [
                    {
                        "addChart": {
                            "chart": {
                                "spec": spec,
                                "position": {
                                    "overlayPosition": {
                                        "anchorCell": {
                                            "sheetId":     sheetId,
                                            "rowIndex":    anchorRow,
                                            "columnIndex": anchorCol,
                                        },
                                        "offsetXPixels": offsetX,
                                        "offsetYPixels": offsetY,
                                        "widthPixels":   width,
                                        "heightPixels":  height,
                                    }
                                },
                            }
                        }
                    }
                ]
            },
        ).execute()

        chart_id = (
            result.get("replies", [{}])[0]
            .get("addChart", {})
            .get("chart", {})
            .get("chartId")
        )
        anchor_cell = f"{index_to_col_letter(anchorCol)}{anchorRow + 1}"

        response = {"success": True, "chartId": chart_id, "anchorCell": anchor_cell}
        logger.info("create_chart response: %s", response)
        return response
    except Exception as e:
        logger.error("Error creating chart: %s", e)
        return {"success": False, "error": str(e)}


def get_charts(service, spreadsheetId: str, sheetId: int) -> dict:
    """
    List all embedded charts on a sheet with their IDs and metadata.

    Returns chartIds, types, data ranges, anchor positions, and titles.
    chartId values are stable — they do not shift when other charts are
    added or deleted.
    Args:
        spreadsheetId: The ID of the target spreadsheet.
        sheetId: Numeric sheet ID to list charts on.

    """
    req = {"spreadsheetId": spreadsheetId, "sheetId": sheetId}
    logger.info("get_charts request: %s", req)

    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheetId,
            fields="sheets(properties.sheetId,charts)",
        ).execute()
    except Exception as e:
        logger.error("Error getting charts: %s", e)
        return {"success": False, "error": str(e)}

    charts = []
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["sheetId"] != sheetId:
            continue
        for chart in sheet.get("charts", []):
            spec = chart.get("spec", {})
            pos  = chart.get("position", {}).get("overlayPosition", {})
            anc  = pos.get("anchorCell", {})
            anchor_cell = (
                f"{index_to_col_letter(anc.get('columnIndex', 0))}"
                f"{anc.get('rowIndex', 0) + 1}"
            )
            data_ranges = []
            if "basicChart" in spec:
                bc = spec["basicChart"]
                chart_type = bc.get("chartType", "UNKNOWN")
                for domain in bc.get("domains", []):
                    for src in domain.get("domain", {}).get("sourceRange", {}).get("sources", []):
                        data_ranges.append(_grid_range_to_a1(src))
                for series in bc.get("series", []):
                    for src in series.get("series", {}).get("sourceRange", {}).get("sources", []):
                        data_ranges.append(_grid_range_to_a1(src))
            elif "pieChart" in spec:
                pc = spec["pieChart"]
                chart_type = "PIE"
                for src in pc.get("domain", {}).get("sourceRange", {}).get("sources", []):
                    data_ranges.append(_grid_range_to_a1(src))
                for src in pc.get("series", {}).get("sourceRange", {}).get("sources", []):
                    data_ranges.append(_grid_range_to_a1(src))
            elif "waterfallChart" in spec:
                wc = spec["waterfallChart"]
                chart_type = "WATERFALL"
                for src in wc.get("domain", {}).get("sourceRange", {}).get("sources", []):
                    data_ranges.append(_grid_range_to_a1(src))
                for s in wc.get("series", []):
                    for src in s.get("data", {}).get("sourceRange", {}).get("sources", []):
                        data_ranges.append(_grid_range_to_a1(src))
            elif "candlestickChart" in spec:
                cc = spec["candlestickChart"]
                chart_type = "CANDLESTICK"
                for src in cc.get("domain", {}).get("data", {}).get("sourceRange", {}).get("sources", []):
                    data_ranges.append(_grid_range_to_a1(src))
                for d in cc.get("data", []):
                    for key in ["lowSeries", "openSeries", "closeSeries", "highSeries"]:
                        for src in d.get(key, {}).get("data", {}).get("sourceRange", {}).get("sources", []):
                            data_ranges.append(_grid_range_to_a1(src))
            else:
                chart_type = "UNKNOWN"

            charts.append({
                "chartId":    chart.get("chartId"),
                "title":      spec.get("title", ""),
                "chartType":  chart_type,
                "anchorCell": anchor_cell,
                "dataRanges": data_ranges,
                "width":      pos.get("widthPixels", 600),
                "height":     pos.get("heightPixels", 371),
            })

    response = {"success": True, "sheetId": sheetId, "charts": charts}
    logger.info("get_charts response: %d charts", len(charts))
    return response


def delete_chart(service, spreadsheetId: str, chartId: int) -> dict:
    """
    Permanently remove an embedded chart from the sheet by its ID.

    Args:
        service: Authenticated Sheets API service (from build_sheets_service).
        spreadsheetId: The ID of the target spreadsheet.
        chartId: Chart ID from get_charts.

    """
    req = {"spreadsheetId": spreadsheetId, "chartId": chartId}
    logger.info("delete_chart request: %s", req)

    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheetId,
            body={"requests": [{"deleteEmbeddedObject": {"objectId": chartId}}]},
        ).execute()

        response = {"success": True, "deletedChartId": chartId}
        logger.info("delete_chart response: %s", response)
        return response
    except Exception as e:
        logger.error("Error deleting chart: %s", e)
        return {"success": False, "error": str(e)}


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
