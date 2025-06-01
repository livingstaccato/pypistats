"""
Python interface to PyPI Stats API
https://pypistats.org/api
"""

from __future__ import annotations

import atexit
import datetime as dt
import json
import sys
import warnings
from pathlib import Path

import structlog
import cattrs
from attrs import define, field # Ensure field is imported if used by models directly here

from rich.console import Console
from rich.table import Table
from rich.text import Text

import plotext

from . import _version
from .models import DownloadStatistic, RecentAPIData, OverallPackageStats, RecentPackageStats
from . import api

logger = structlog.get_logger(__name__)
__version__ = _version.__version__

atexit.register(api._clear_cache)

# Moved from being locally defined in pypi_stats_api
@define
class TempRecentDisplayItem:
    """Helper class for displaying 'recent' stats consistently with other table data."""
    category: str
    last_day: int
    last_week: int | None = None
    last_month: int | None = None

# --- Processing Helper Functions (retained from previous state) ---
def _validate_total(total: str) -> None:
    supported_granularities = ("daily", "monthly", "all")
    if total not in supported_granularities:
        msg = f"total must be one of {supported_granularities}"
        raise ValueError(msg)

def _filter(data: list[DownloadStatistic], start_date: str | None = None, end_date: str | None = None) -> list[DownloadStatistic]:
    current_data = data
    if start_date:
        current_data = [item for item in current_data if item.date >= start_date]
    if end_date:
        current_data = [item for item in current_data if item.date <= end_date]
    return current_data

def _sort(data: list[DownloadStatistic | TempRecentDisplayItem]) -> list[DownloadStatistic | TempRecentDisplayItem]:
    if not data: return []
    first_item = data[0]
    if isinstance(first_item, DownloadStatistic):
        return sorted([item for item in data if isinstance(item, DownloadStatistic)], key=lambda item: item.downloads, reverse=True)
    elif isinstance(first_item, TempRecentDisplayItem):
        return sorted([item for item in data if isinstance(item, TempRecentDisplayItem)], key=lambda item: item.last_day, reverse=True)
    logger.warn("Attempted to sort a list of unknown or mixed item types not handled explicitly.", first_item_type=type(first_item))
    return data

def _monthly_total(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
    totalled: dict[str, dict[str, int]] = {}
    for item in data:
        category = item.category
        downloads = item.downloads
        month = item.date[:7]
        if category not in totalled: totalled[category] = {}
        totalled[category][month] = totalled[category].get(month, 0) + downloads
    new_data: list[DownloadStatistic] = []
    for category, month_downloads in totalled.items():
        for month, downloads_val in month_downloads.items():
            new_data.append(DownloadStatistic(category=category, date=month, downloads=downloads_val))
    return new_data

def _total(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
    totalled: dict[str, int] = {}
    for item in data:
        totalled[item.category] = totalled.get(item.category, 0) + item.downloads
    new_data: list[DownloadStatistic] = []
    for category, downloads_val in totalled.items():
        new_data.append(DownloadStatistic(category=category, date="aggregated", downloads=downloads_val))
    return new_data

def _date_range(data: list[DownloadStatistic]) -> tuple[str | None, str | None]:
    dates = [item.date for item in data if item.date is not None]
    if not dates: return None, None
    return min(dates), max(dates)

def _grand_total_value(data: list[DownloadStatistic]) -> int:
    if data and isinstance(data[0], DownloadStatistic) and data[0].category in ["with_mirrors", "without_mirrors"]:
        count_with_mirrors = sum(item.downloads for item in data if item.category == "with_mirrors")
        count_without_mirrors = sum(item.downloads for item in data if item.category == "without_mirrors")
        return max(count_with_mirrors, count_without_mirrors)
    return sum(item.downloads for item in data)

def _grand_total(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
    if not data or len(data) == 1 and not isinstance(data[0], DownloadStatistic): return data # Guard against non-DownloadStatistic
    if not data or not all(isinstance(d, DownloadStatistic) for d in data) or len(data) ==1: return data

    grand_total_val = _grand_total_value(data)
    new_row = DownloadStatistic(category="Total", date="aggregated", downloads=grand_total_val)
    return data + [new_row]

def _percent(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
    if not data or len(data) == 1 and not isinstance(data[0], DownloadStatistic): return data
    if not data or not all(isinstance(d, DownloadStatistic) for d in data) or len(data) == 1: return data

    grand_total_val = _grand_total_value(data)
    if grand_total_val == 0:
        for item in data: item.percent = "0.00%"
        return data
    for item in data: item.percent = "{:.2%}".format(item.downloads / grand_total_val)
    return data

# --- Display functions ---
def _tabulate_rich_pretty(data_list: list[DownloadStatistic | TempRecentDisplayItem], package_name: str, console: Console) -> None:
    if not data_list:
        console.print(f"No data to display for {package_name}.")
        return
    table = Table(title=f"Stats for {package_name}", show_lines=True)
    first_item = data_list[0]
    headers: list[str] = []
    if isinstance(first_item, DownloadStatistic):
        headers = ["category", "date", "downloads"]
        if any(isinstance(it, DownloadStatistic) and it.percent is not None for it in data_list):
            headers.append("percent")
    elif isinstance(first_item, TempRecentDisplayItem):
        headers = ["category", "last_day", "last_week", "last_month"]
    else:
        item_type_name = type(first_item).__name__
        logger.error(f"Unknown item type for pretty tabulation: {item_type_name}", item_preview=str(first_item)[:100])
        console.print("[red]Error: Unknown data type for table display.[/red]")
        return
    for header_name in headers:
        justify_style = "left" if header_name in ["category", "date"] else "right"
        table.add_column(header_name.replace("_", " ").title(), justify=justify_style)
    for item in data_list:
        row_values_for_rich: list[str | Text] = []
        for header_name in headers:
            value = getattr(item, header_name, "")
            current_cell_content: str | Text
            if value is None: current_cell_content = ""
            elif header_name in ["downloads", "last_day", "last_week", "last_month"] and isinstance(value, int):
                current_cell_content = f"{value:,}"
            elif header_name == "percent" and isinstance(value, str):
                try:
                    percent_val_float = float(value.rstrip('%'))
                    style_color = "green"
                    if percent_val_float <= 5.0: style_color = "red"
                    elif percent_val_float <= 15.0: style_color = "yellow"
                    current_cell_content = Text(value, style=style_color)
                except ValueError: current_cell_content = value
            else: current_cell_content = str(value)
            row_values_for_rich.append(current_cell_content)
        table.add_row(*row_values_for_rich)
    console.print(table)

def _generate_plotext_plot(data_list: list[DownloadStatistic], package_name: str, console: Console) -> None:
    if not data_list:
        console.print(f"No data to plot for {package_name}.")
        return
    plot_data = [{'date': item.date, 'downloads': item.downloads, 'category': item.category}
                 for item in data_list if isinstance(item, DownloadStatistic) and hasattr(item, 'date') and isinstance(item.downloads, int)]
    if not plot_data:
        console.print(f"No suitable data points found for plotting for {package_name}.")
        return
    try:
        plot_data.sort(key=lambda x: x['date'])
    except TypeError as e:
        logger.error("Failed to sort plot data by date.", error=str(e), first_date_example=plot_data[0]['date'] if plot_data else "N/A")
        console.print("[red]Error: Could not sort data by date for plotting.[/red]")
        return
    categories = sorted(list(set(item['category'] for item in plot_data)))
    plot_title = f"Download Stats for {package_name}"
    if len(categories) == 1: plot_title += f" ({categories[0]})"
    elif len(categories) > 1: plot_title += " (Multiple Categories)"
    dates_str = [item['date'] for item in plot_data]
    downloads_list = [item['downloads'] for item in plot_data]
    plotext.clear_figure()
    plotext.plot_date(dates_str, downloads_list)
    plotext.title(plot_title)
    plotext.xlabel("Date")
    plotext.ylabel("Downloads")
    plotext.show()

def _pytablewriter(headers: list[str], data: list[dict], format_: str):
    from pytablewriter import (HtmlTableWriter, MarkdownTableWriter, NumpyTableWriter,
                               PandasDataFrameWriter, RstSimpleTableWriter, String, TsvTableWriter)
    from pytablewriter.style import Align, Style, ThousandSeparator
    format_writers = {"html": HtmlTableWriter, "numpy": NumpyTableWriter, "pandas": PandasDataFrameWriter,
                      "rst": RstSimpleTableWriter, "tsv": TsvTableWriter, "md": MarkdownTableWriter,
                      "markdown": MarkdownTableWriter}
    writer = format_writers[format_]()
    if format_ != "html": writer.margin = 1
    if isinstance(data, dict): writer.value_matrix = [data] # Should ideally be list[dict]
    else: writer.value_matrix = data
    writer.headers = headers
    if not headers and data: writer.headers = list(data[0].keys()) if data and data[0] else []
    if writer.headers:
        if writer.headers and writer.headers[0] in ["last_day", "last_month", "last_week"]: # Check if headers is not empty
            writer.column_styles = len(writer.headers) * [Style(thousand_separator=",")]
        else:
            column_styles, type_hints = [], []
            for header in writer.headers:
                align = Align.AUTO
                thousand_separator = ThousandSeparator.NONE
                type_hint = None
                if header == "percent": align = Align.RIGHT
                elif header == "downloads" and (format_ not in ["numpy", "pandas"]):
                    thousand_separator = ThousandSeparator.COMMA
                elif header == "category": type_hint = String
                style = Style(align=align, thousand_separator=thousand_separator)
                column_styles.append(style)
                type_hints.append(type_hint)
            writer.column_styles = column_styles
            writer.type_hints = type_hints
    if format_ == "numpy": return writer.tabledata.as_dataframe().values
    elif format_ == "pandas": return writer.tabledata.as_dataframe()
    return writer.dumps()

# --- Public Functions ---
def _common_api_orchestrator(
    package: str,
    endpoint_template: str,
    api_params: dict,
    format: str | None,
    total: str | None, # Only for OverallStats
    color: str,
    verbose: bool,
    start_date: str | None = None, # For cache key and date range footer
    end_date: str | None = None    # For cache key and date range footer
):
    if format == "md": # Normalize "md" to "markdown" early
        format = "markdown"

    endpoint = endpoint_template.format(package=package)
    params_list = [api._paramify(k, v) for k, v in api_params.items() if v is not None]
    params_str = "".join(params_list)
    if params_str: params_str = "?" + params_str.lstrip("&")


    api_result = api.pypi_stats_api(endpoint, params=params_str, start_date=start_date, end_date=end_date, verbose=verbose)

    if isinstance(api_result, str): return api_result # Error string

    converter = cattrs.Converter()

    # Prepare data for display/processing
    data_for_display: list[DownloadStatistic | TempRecentDisplayItem]
    display_package_name = api_result.package
    first_api_date: str | None = None
    last_api_date: str | None = None
    plot_source_data: list[DownloadStatistic] = []


    if isinstance(api_result, RecentPackageStats):
        display_item = TempRecentDisplayItem(
            category=api_result.package,
            last_day=api_result.data.last_day,
            last_week=api_result.data.last_week,
            last_month=api_result.data.last_month
        )
        data_for_display = [display_item]
    elif isinstance(api_result, OverallPackageStats):
        processed_data = list(api_result.data)
        first_api_date, last_api_date = _date_range(processed_data)

        # User-provided dates for footer take precedence if stricter
        if start_date: first_api_date = max(first_api_date, start_date) if first_api_date else start_date
        if end_date: last_api_date = min(last_api_date, end_date) if last_api_date else end_date

        # Filtering for display and plotting source
        if start_date or end_date:
             # Validate against actual data range from API before filtering
            data_first_date, data_last_date = _date_range(api_result.data) # Use original data for this check
            if end_date and data_first_date and end_date < data_first_date:
                raise ValueError(f"Requested end date ({end_date}) is before earliest available data ({data_first_date}).")
            if start_date and data_first_date and start_date < data_first_date: # Check against data_first_date not first_api_date
                warnings.warn(f"Requested start date ({start_date}) is before earliest available data ({data_first_date}).", stacklevel=2)

            processed_data = _filter(processed_data, start_date, end_date)

        plot_source_data = list(processed_data) # Data for plotting is after date filtering

        if total: # total is guaranteed to be valid by CLI or _validate_total in public func
            _validate_total(total) # Still good to have if called programmatically
            if total == "monthly":
                processed_data = _monthly_total(processed_data)
            elif total == "all":
                processed_data = _total(processed_data)

        # Update api_result.data if aggregations were done, for JSON output consistency
        if total in ("monthly", "all"):
            api_result.data = processed_data

        data_for_display = _sort(list(processed_data)) # Sort the (potentially aggregated) data

        if format not in ("plot", "json"): # Percent & grand_total not for plot or raw JSON here
            if data_for_display and all(isinstance(item, DownloadStatistic) for item in data_for_display):
                data_for_display = _percent(data_for_display)
                data_for_display = _grand_total(data_for_display)
    else:
        return "Error: Unknown API response type after structuring."

    if format == "json":
        return json.dumps(converter.unstructure(api_result))

    force_terminal: bool | None = None
    if color == "yes": force_terminal = True
    elif color == "no": force_terminal = False
    console = Console(force_terminal=force_terminal)

    if format == "pretty":
        _tabulate_rich_pretty(data_for_display, display_package_name, console)
        return ""
    elif format == "plot":
        if isinstance(api_result, OverallPackageStats):
            _generate_plotext_plot(plot_source_data, display_package_name, console)
        else:
            console.print("[yellow]Plotting is not applicable for 'recent' stats.[/yellow]")
        return ""
    elif format in ("html", "markdown", "rst", "tsv"):
        if not data_for_display: return "No data to tabulate."
        data_as_dicts = [converter.unstructure(item) for item in data_for_display]
        headers_list : list[str] = [] # Renamed variable
        if data_as_dicts:
            first_item_obj = data_for_display[0]
            if isinstance(first_item_obj, DownloadStatistic):
                headers_list = ["category", "date", "downloads"]
                if any(getattr(it, 'percent', None) is not None for it in data_for_display):
                    headers_list.append("percent")
            elif isinstance(first_item_obj, TempRecentDisplayItem):
                headers_list = ["category", "last_day", "last_week", "last_month"]

        table_str = _pytablewriter(headers_list, data_as_dicts, format)
        if first_api_date and last_api_date and isinstance(api_result, OverallPackageStats):
             return f"{table_str}\nDate range: {first_api_date} - {last_api_date}\n"
        return table_str

    # Fallback for format=None or any other unhandled format from CLI (though Click should prevent this)
    if format is None: # Handle case where format might be None if default is removed in CLI
        # Return the processed data_for_display list (attrs objects)
        return data_for_display # Or unstructure it: [converter.unstructure(item) for item in data_for_display]

    return f"Unknown format: {format}"


def recent(package: str, period: str | None = None, *, format: str | None = "pretty", verbose: bool = False, color: str = "auto"):
    return _common_api_orchestrator(
        package=package,
        endpoint_template="packages/{package}/recent",
        api_params={"period": period},
        format=format,
        total=None, # Not applicable for recent
        color=color,
        verbose=verbose
    )

def overall(package: str, *, mirrors: bool | None = None, start_date: str | None = None, end_date: str | None = None,
            format: str | None = "pretty", total: str = "all", color: str = "auto", verbose: bool = False):
    return _common_api_orchestrator(
        package=package,
        endpoint_template="packages/{package}/overall",
        api_params={"mirrors": mirrors},
        format=format,
        total=total,
        color=color,
        verbose=verbose,
        start_date=start_date,
        end_date=end_date
    )

def python_major(package: str, version: str | None = None, *, start_date: str | None = None, end_date: str | None = None,
                 format: str | None = "pretty", total: str = "all", color: str = "auto", verbose: bool = False):
    return _common_api_orchestrator(
        package=package,
        endpoint_template="packages/{package}/python_major",
        api_params={"version": version},
        format=format,
        total=total,
        color=color,
        verbose=verbose,
        start_date=start_date,
        end_date=end_date
    )

def python_minor(package: str, version: str | None = None, *, start_date: str | None = None, end_date: str | None = None,
                 format: str | None = "pretty", total: str = "all", color: str = "auto", verbose: bool = False) -> str:
    # Explicitly typing return as str for now, though common orchestrator might return list
    result = _common_api_orchestrator(
        package=package,
        endpoint_template="packages/{package}/python_minor",
        api_params={"version": version},
        format=format,
        total=total,
        color=color,
        verbose=verbose,
        start_date=start_date,
        end_date=end_date
    )
    if not isinstance(result, str): # Should not happen if format is not None
        return str(result) # Fallback conversion
    return result


def system(package: str, os: str | None = None, *, start_date: str | None = None, end_date: str | None = None,
           format: str | None = "pretty", total: str = "all", color: str = "auto", verbose: bool = False):
    return _common_api_orchestrator(
        package=package,
        endpoint_template="packages/{package}/system",
        api_params={"os": os},
        format=format,
        total=total,
        color=color,
        verbose=verbose,
        start_date=start_date,
        end_date=end_date
    )
