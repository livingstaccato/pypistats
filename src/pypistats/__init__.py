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
from platformdirs import user_cache_dir
from slugify import slugify
# from termcolor import colored # Removed

from rich.console import Console
from rich.table import Table
from rich.text import Text # For colored text
import plotext

from . import _version
from .models import DownloadStatistic, RecentAPIData, OverallPackageStats, RecentPackageStats # Updated imports
from attrs import define # For TempRecentDisplayItem
import cattrs

# Basic structlog configuration for console output
structlog.configure(
    processors=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger(__name__)

__version__ = _version.__version__

BASE_URL = "https://pypistats.org/api/"
CACHE_DIR = Path(user_cache_dir("pypistats"))
USER_AGENT = f"pypistats/{__version__}"


def _cache_filename(url: str) -> Path:
    """yyyy-mm-dd-url-slug.json"""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    slug = slugify(url)
    filename = CACHE_DIR / f"{today}-{slug}.json"

    return filename


def _load_cache(cache_file: Path) -> dict:
    if not cache_file.exists():
        return {}

    with cache_file.open("r") as f:
        try:
            data = json.load(f)
        except json.decoder.JSONDecodeError:
            return {}

    return data


def _save_cache(cache_file: Path, data) -> None:
    try:
        if not CACHE_DIR.exists():
            CACHE_DIR.mkdir(parents=True)

        with cache_file.open("w") as f:
            json.dump(data, f)

    except OSError:
        pass


def _clear_cache() -> None:
    """Delete old cache files, run as last task"""
    cache_files = CACHE_DIR.glob("**/*.json")
    this_month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    for cache_file in cache_files:
        if not cache_file.name.startswith(this_month):
            cache_file.unlink()


atexit.register(_clear_cache)


def _validate_total(total: str) -> None:
    supported_granularities = ("daily", "monthly", "all")
    if total not in supported_granularities:
        msg = f"total must be one of {supported_granularities}"
        raise ValueError(msg)


def pypi_stats_api(
    endpoint: str,
    params: str | None = None,
    format: str | None = "pretty",
    start_date: str | None = None,
    end_date: str | None = None,
    sort: bool = True,
    total: str = "all",
    color_param: str = "auto", # Renamed from 'color', default from CLI
    verbose: bool = False,
):
    """Call the API and return JSON"""
    _validate_total(total)
    if format == "md": # click choices are case-insensitive, but internal logic might expect "markdown"
        format = "markdown"
    if params:
        params = "?" + params
    else:
        params = ""
    url = BASE_URL + endpoint.lower() + params
    cache_file = _cache_filename(url)
    if verbose: logger.debug("API URL", url=url)
    if verbose: logger.debug("Cache file", path=str(cache_file))

    raw_api_response_dict = {} # Initialize

    # Try loading from cache first
    if cache_file.is_file():
        if verbose: logger.debug("Cache file exists, loading.", cache_file_path=str(cache_file))
        raw_api_response_dict = _load_cache(cache_file)
        if not raw_api_response_dict:
            if verbose: logger.debug("Cache was empty or invalid.")
            # If cache is invalid/empty, raw_api_response_dict remains {} or what _load_cache returned (e.g. {})
            # This will trigger network fetch next.

    if not raw_api_response_dict: # If not found in cache or cache was invalid
        import httpx # Keep import here to avoid top-level if not always needed
        if verbose: logger.debug("Fetching from network.", url=url)

        response = httpx.get(url, headers={"User-Agent": USER_AGENT})

        if verbose:
            logger.debug("HTTP status code received.", status_code=response.status_code, url=url)
        response.raise_for_status() # Raise HTTP errors

        raw_api_response_dict = response.json()
        _save_cache(cache_file, raw_api_response_dict) # Cache the raw dictionary

    # Now, raw_api_response_dict contains the data as a dictionary.
    # Check for empty or missing 'data' key before attempting to structure.
    if not raw_api_response_dict or not raw_api_response_dict.get("data"):
        package_name_for_msg = raw_api_response_dict.get("package", "") if raw_api_response_dict else ""
        logger.warn("No data found for package in API response.", package_name=package_name_for_msg, api_url=url)
        # This matches original behavior of returning a string message.
        return f"No data found for https://pypi.org/project/{package_name_for_msg}/"

    # Initialize cattrs converter
    converter = cattrs.Converter()

    # Decide which top-level model to use for structuring
    structured_response: OverallPackageStats | RecentPackageStats # Union type for the variable

    # Infer endpoint type (this is a simplified check, might need refinement)
    is_recent_endpoint = "recent" in endpoint.lower()

    try:
        if is_recent_endpoint:
            structured_response = converter.structure(raw_api_response_dict, RecentPackageStats)
        else: # Assume OverallPackageStats for other endpoints
            structured_response = converter.structure(raw_api_response_dict, OverallPackageStats)

    except Exception as e:
        package_name_for_error = raw_api_response_dict.get("package", "unknown package")
        logger.error(
            "Failed to structure API response with cattrs.",
            package_name=package_name_for_error,
            error=str(e),
            response_preview="".join(str(raw_api_response_dict)[:200].splitlines()),
            api_url=url
        )
        return f"Error processing API data for {package_name_for_error}: {str(e)}"

    # JSON output needs to unstructure the correct type
    if format == "json":
        return json.dumps(converter.unstructure(structured_response))

    # Define TempRecentDisplayItem for consistent tabulation of "recent" data
    @define
    class TempRecentDisplayItem:
        category: str
        last_day: int
        last_week: int | None = None
        last_month: int | None = None

    data_for_tabulation: list[DownloadStatistic | TempRecentDisplayItem]
    first: str | None = None
    last: str | None = None
    plot_data_source: list[DownloadStatistic] = [] # Initialize for plot if OverallPackageStats

    if isinstance(structured_response, RecentPackageStats):
        temp_item = TempRecentDisplayItem(
            category=structured_response.package,
            last_day=structured_response.data.last_day,
            last_week=structured_response.data.last_week,
            last_month=structured_response.data.last_month
        )
        data_for_tabulation = [temp_item]
        # Date range is not applicable for recent stats.
        first, last = None, None

    elif isinstance(structured_response, OverallPackageStats):
        data_list_stats = structured_response.data # list[DownloadStatistic]

        # Apply filtering, sorting, aggregation if it's this type of data
        first, last = _date_range(data_list_stats) # _date_range expects list of DownloadStatistic

        if end_date and first and end_date < first:
            raise ValueError(f"Requested end date ({end_date}) is before earliest available data ({first}).")
        if start_date and first and start_date < first:
            warnings.warn(f"Requested start date ({start_date}) is before earliest available data ({first}).", stacklevel=3)

        if start_date or end_date:
            data_list_stats = _filter(data_list_stats, start_date, end_date)

        # Store data for plotting before aggregation by _total or _monthly_total
        plot_data_source = list(data_list_stats)

        if total == "monthly":
            data_list_stats = _monthly_total(data_list_stats)
        elif total == "all":
            data_list_stats = _total(data_list_stats)

        data_for_tabulation = data_list_stats

        if sort:
            data_for_tabulation = _sort(data_for_tabulation)

        if format != "plot": # For plot, use plot_data_source before these aggregations
            if isinstance(data_for_tabulation, list) and \
               all(isinstance(item, DownloadStatistic) for item in data_for_tabulation):
                data_for_tabulation = _percent(data_for_tabulation)
                data_for_tabulation = _grand_total(data_for_tabulation)
    else:
        # Should not happen due to endpoint check earlier
        logger.error("Unknown structured_response type", type=type(structured_response).__name__)
        return "Internal error: Could not determine response structure type."


    # --- Output Formatting Stage ---
    # 'data_for_tabulation' is now a list of either DownloadStatistic or TempRecentDisplayItem.

    output: str = ""
    force_terminal: bool | None = None
    if color_param == "yes": force_terminal = True
    elif color_param == "no": force_terminal = False

    console = Console(force_terminal=force_terminal)

    if format == "pretty":
        _tabulate_rich_pretty(data_for_tabulation, structured_response.package, console)
        return ""

    elif format == "plot":
        if isinstance(structured_response, OverallPackageStats):
            # Use plot_data_source which is after date filtering but before _total/_monthly_total aggregation
            _generate_plotext_plot(plot_data_source, structured_response.package, console)
        else: # RecentPackageStats
            console.print("[yellow]Plotting is not applicable for 'recent' stats.[/yellow]")
        return ""

    elif format in ("html", "markdown", "rst", "tsv"):
        if not data_for_tabulation:
            return "No data to tabulate."

        # cattrs converter should be the same instance used for structuring
        data_as_dicts = [converter.unstructure(item) for item in data_for_tabulation]

        headers: list[str] = []
        if data_as_dicts:
            first_item_for_header = data_for_tabulation[0] # Check original type
            if isinstance(first_item_for_header, DownloadStatistic):
                headers = ["category", "date", "downloads"]
                if hasattr(first_item_for_header, 'percent') and first_item_for_header.percent is not None:
                    headers.append("percent")
            elif isinstance(first_item_for_header, TempRecentDisplayItem): # Check Temp object
                headers = ["category", "last_day", "last_week", "last_month"]

        output = _pytablewriter(headers, data_as_dicts, format)

    else:
        logger.warn("Unknown or unhandled format requested for tabulation", requested_format=format)
        return f"Format '{format}' not supported by this path."

    if first and format not in ["numpy", "pandas", "pretty", "plot"]:
        return f"{output}\nDate range: {first} - {last}\n"
    else:
        return output


def _generate_plotext_plot(
    data_list: list[DownloadStatistic],
    package_name: str,
    console: Console
) -> None:
    """Generates and displays a terminal plot using plotext."""

    if not data_list:
        console.print(f"No data to plot for {package_name}.")
        return

    plot_data = []
    for item in data_list:
        if isinstance(item, DownloadStatistic) and hasattr(item, 'date') and isinstance(item.downloads, int):
            try:
                plot_data.append({'date': item.date, 'downloads': item.downloads, 'category': item.category})
            except (ValueError, TypeError): # Should not happen if DownloadStatistic is well-formed
                logger.warn("Skipping item with invalid data for plotting.", item_details=str(item))
                continue

    if not plot_data:
        console.print(f"No suitable data points found for plotting for {package_name}.")
        return

    try:
        plot_data.sort(key=lambda x: x['date'])
    except TypeError as e: # Should not happen if dates are consistently YYYY-MM or YYYY-MM-DD strings
        logger.error("Failed to sort plot data by date.", error=str(e), first_date_example=plot_data[0]['date'] if plot_data else "N/A")
        console.print("[red]Error: Could not sort data by date for plotting.[/red]")
        return

    categories = sorted(list(set(item['category'] for item in plot_data)))

    plot_title = f"Download Stats for {package_name}"
    if len(categories) == 1:
        plot_title += f" ({categories[0]})"
    elif len(categories) > 1:
         plot_title += " (Multiple Categories)"

    dates_str = [item['date'] for item in plot_data]
    downloads_list = [item['downloads'] for item in plot_data]

    plotext.clear_figure()
    plotext.plot_date(dates_str, downloads_list)

    plotext.title(plot_title)
    plotext.xlabel("Date")
    plotext.ylabel("Downloads")

    plotext.show()

def _filter(
    data: list[DownloadStatistic],
    start_date: str | None = None,
    end_date: str | None = None
) -> list[DownloadStatistic]:
    """Only return data with dates between start_date and end_date."""
    # This function primarily applies to DownloadStatistic objects which have a 'date'
    current_data = data

    if start_date:
        current_data = [item for item in current_data if item.date >= start_date]

    if end_date:
        current_data = [item for item in current_data if item.date <= end_date]

    return current_data


def _sort(data: list[DownloadStatistic | TempRecentDisplayItem]) -> list[DownloadStatistic | TempRecentDisplayItem]:
    """Sort by downloads or last_day. Handles DownloadStatistic or TempRecentDisplayItem."""

    if not data:
        return []

    first_item = data[0]
    if isinstance(first_item, DownloadStatistic):
        # Ensure all items are DownloadStatistic if sorting by downloads
        return sorted([item for item in data if isinstance(item, DownloadStatistic)], key=lambda item: item.downloads, reverse=True)
    elif isinstance(first_item, TempRecentDisplayItem):
         # Ensure all items are TempRecentDisplayItem if sorting by last_day
        return sorted([item for item in data if isinstance(item, TempRecentDisplayItem)], key=lambda item: item.last_day, reverse=True)

    logger.warn("Attempted to sort a list of unknown or mixed item types not handled explicitly.", first_item_type=type(first_item))
    return data # Return original data if type is not recognized for sorting


def _monthly_total(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
    """Sum all downloads per category, by month, for DownloadStatistic items."""
    totalled: dict[str, dict[str, int]] = {}
    for item in data:
        if not isinstance(item, DownloadStatistic): # Skip if not a DownloadStatistic
            continue

        category = item.category
        downloads = item.downloads
        month = item.date[:7] # Assumes date is YYYY-MM-DD

        if category not in totalled:
            totalled[category] = {}

        totalled[category][month] = totalled[category].get(month, 0) + downloads

    new_data: list[DownloadStatistic] = []
    for category, month_downloads in totalled.items():
        for month, downloads in month_downloads.items():
            new_data.append(DownloadStatistic(category=category, date=month, downloads=downloads))

    return new_data


def _total(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
    """Sum all downloads per category, regardless of date, for DownloadStatistic items."""
    totalled: dict[str, int] = {} # category -> total_downloads
    for item in data:
        if not isinstance(item, DownloadStatistic):
            continue
        totalled[item.category] = totalled.get(item.category, 0) + item.downloads

    new_data: list[DownloadStatistic] = []
    # The original _total returned items with 'category' and 'downloads', but no 'date'.
    # To fit the DownloadStatistic model, we might need a different model or use a placeholder date.
    # For now, let's use a placeholder date like "aggregated" or an empty string.
    # This is a point that might need refinement based on how this data is used later.
    for category, downloads in totalled.items():
        new_data.append(DownloadStatistic(category=category, date="aggregated", downloads=downloads)) # percent will be None by default


    return new_data


def _date_range(data: list[DownloadStatistic]) -> tuple[str | None, str | None]:
    """Return the first and last dates in data if items have a 'date' attribute."""
    # Assumes data is list[DownloadStatistic] and items have a 'date' attribute.
    dates = [item.date for item in data if item.date is not None]

    if not dates:
        return None, None

    # Assuming date strings are in a comparable format like YYYY-MM-DD or YYYY-MM
    first = min(dates)
    last = max(dates)

    return first, last


def _grand_total_value(data: list[DownloadStatistic]) -> int:
    """Return the grand total of the data for DownloadStatistic items."""
    # Original logic for "overall" with mirrors needs to be preserved.
    # This means checking item.category values.
    if data and isinstance(data[0], DownloadStatistic) and data[0].category in ["with_mirrors", "without_mirrors"]:
        count_with_mirrors = sum(
            item.downloads for item in data if isinstance(item, DownloadStatistic) and item.category == "with_mirrors"
        )
        count_without_mirrors = sum(
            item.downloads for item in data if isinstance(item, DownloadStatistic) and item.category == "without_mirrors"
        )
        return max(count_with_mirrors, count_without_mirrors)
    else:
        return sum(item.downloads for item in data if isinstance(item, DownloadStatistic))

def _grand_total(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
    """Add a grand total row for DownloadStatistic items."""
    if not data or not isinstance(data[0], DownloadStatistic) or len(data) == 1:
        return data # No need or not applicable

    grand_total = _grand_total_value(data)
    # Similar to _total, the "Total" row needs a date. Using "aggregated".
    new_row = DownloadStatistic(category="Total", date="aggregated", downloads=grand_total) # percent will be None

    # Return a new list with the total row appended
    return data + [new_row]


def _percent(data: list[DownloadStatistic]) -> list[DownloadStatistic]: # Signature fine
    """Add a percent value to each DownloadStatistic item. Modifies items in place."""
    if not data or not isinstance(data[0], DownloadStatistic) or len(data) == 1:
        return data

    grand_total = _grand_total_value(data) # Uses the updated one

    if grand_total == 0: # Avoid division by zero
        for item in data:
            if isinstance(item, DownloadStatistic):
                item.percent = "0.00%" # Or some other representation for zero total
        return data

    for item in data:
        if isinstance(item, DownloadStatistic):
            # Ensure DownloadStatistic has a 'percent' attribute and is mutable
            # This subtask assumes 'percent' field exists and model is mutable.
            item.percent = "{:.2%}".format(item.downloads / grand_total)

def _tabulate_rich_pretty(
    data_list: list[DownloadStatistic | TempRecentDisplayItem],
    package_name: str,
    console: Console
) -> None:
    """Generates and prints a 'pretty' table using rich.table.Table."""

    if not data_list:
        console.print(f"No data to display for {package_name}.")
        return

    table = Table(title=f"Stats for {package_name}", show_lines=True)

    first_item = data_list[0]
    headers: list[str] = []

    if isinstance(first_item, DownloadStatistic):
        headers = ["category", "date", "downloads"]
        # Check if any item has a 'percent' to decide if column should be added
        if any(isinstance(it, DownloadStatistic) and it.percent is not None for it in data_list):
            headers.append("percent")
    elif isinstance(first_item, TempRecentDisplayItem):
        headers = ["category", "last_day", "last_week", "last_month"]
    else:
        console.print(f"[red]Error: Unknown data type for table display: {type(first_item)}[/red]")
        return

    for header_name in headers: # Renamed to avoid conflict
        table.add_column(header_name.replace("_", " ").title(), justify="right" if header_name not in ["category", "date"] else "left")

    for item in data_list:
        row_values: list[str | Text] = []
        for header_name in headers: # Renamed to avoid conflict
            value = getattr(item, header_name, None)

            value_str: str | Text
            if value is None and header_name in ["last_week", "last_month", "percent"]:
                 value_str = ""
            elif header_name in ["downloads", "last_day", "last_week", "last_month"] and isinstance(value, int):
                value_str = f"{value:,}"
            elif header_name == "percent" and isinstance(value, str):
                try:
                    percent_val = float(value.rstrip('%'))
                    style = "green"
                    if percent_val <= 5: style = "red"
                    elif percent_val <= 15: style = "yellow"
                    value_str = Text(value, style=style)
                except ValueError:
                    value_str = str(value)
            else:
                value_str = str(value)

            row_values.append(value_str)
        table.add_row(*row_values)

    console.print(table)

def _pytablewriter(headers: list[str], data: list[dict], format_: str): # Signature fine

            # Formatting and coloring
            if header == "downloads" and isinstance(value, int):
                value_str = f"{value:,}"
            elif header == "last_day" and isinstance(value, int): # For RecentStats
                value_str = f"{value:,}"
            elif header == "last_week" and isinstance(value, int): # For RecentStats
                value_str = f"{value:,}"
            elif header == "last_month" and isinstance(value, int): # For RecentStats
                value_str = f"{value:,}"
            elif header == "percent" and isinstance(value, str):
                # Apply color based on percentage value (example logic)
                try:
                    percent_val = float(value.rstrip('%'))
                    if percent_val <= 5: style = "red"
                    elif percent_val <= 15: style = "yellow"
                    else: style = "green"
                    value_str = Text(value, style=style)
                except ValueError:
                    value_str = str(value) # Fallback
            else:
                value_str = str(value)

            row_values.append(value_str)
        table.add_row(*row_values)

    console.print(table)

def _pytablewriter(headers: list[str], data: list[dict], format_: str):
    from pytablewriter import (
        HtmlTableWriter,
        NumpyTableWriter,
        PandasDataFrameWriter,
        RstSimpleTableWriter,
        String,
        TsvTableWriter,
    )
    from pytablewriter.style import Align, Style, ThousandSeparator

    format_writers = {
        "html": HtmlTableWriter,
        "numpy": NumpyTableWriter,
        "pandas": PandasDataFrameWriter,
        "rst": RstSimpleTableWriter,
        "tsv": TsvTableWriter,
    }

    writer = format_writers[format_]()
    if format_ != "html":
        writer.margin = 1

    if isinstance(data, dict):
        writer.value_matrix = [data]
    else:  # isinstance(data, list):
        writer.value_matrix = data

    writer.headers = headers

    # Custom alignment and format
    if headers[0] in ["last_day", "last_month", "last_week"]:
        # Special case for 'recent'
        writer.column_styles = len(headers) * [Style(thousand_separator=",")]
    else:
        column_styles = []
        type_hints = []

        for header in headers:
            align = Align.AUTO
            thousand_separator = ThousandSeparator.NONE
            type_hint = None
            if header == "percent":
                align = Align.RIGHT
            elif header == "downloads" and (format_ not in ["numpy", "pandas"]):
                thousand_separator = ThousandSeparator.COMMA
            elif header == "category":
                type_hint = String
            style = Style(align=align, thousand_separator=thousand_separator)
            column_styles.append(style)
            type_hints.append(type_hint)

        writer.column_styles = column_styles
        writer.type_hints = type_hints

    if format_ == "numpy":
        return writer.tabledata.as_dataframe().values
    elif format_ == "pandas":
        return writer.tabledata.as_dataframe()
    return writer.dumps()


def _paramify(param_name: str, param_value: float | str | None) -> str:
    """If param_value, return &param_name=param_value"""
    if isinstance(param_value, bool):
        param_value = str(param_value).lower()

    if param_value:
        return "&" + param_name + "=" + str(param_value)

    return ""


def recent(package: str, period: str | None = None, **kwargs: str):
    """Retrieve the aggregate download quantities for the last 1/7/30 days,
    excluding downloads from mirrors"""
    endpoint = f"packages/{package}/recent"
    params = _paramify("period", period)
    return pypi_stats_api(endpoint, params, **kwargs)


def overall(package: str, mirrors: bool | str | None = None, **kwargs: str):
    """Retrieve the aggregate daily download time series with or without mirror
    downloads"""
    endpoint = f"packages/{package}/overall"
    params = _paramify("mirrors", mirrors)
    return pypi_stats_api(endpoint, params, **kwargs)


def python_major(package: str, version: str | None = None, **kwargs: str):
    """Retrieve the aggregate daily download time series by Python major version
    number"""
    endpoint = f"packages/{package}/python_major"
    params = _paramify("version", version)
    return pypi_stats_api(endpoint, params, **kwargs)


def python_minor(package: str, version: str | None = None, **kwargs) -> str:
    """Retrieve the aggregate daily download time series by Python minor version
    number"""
    endpoint = f"packages/{package}/python_minor"
    params = _paramify("version", version)
    return pypi_stats_api(endpoint, params, **kwargs)


def system(package: str, os: str | None = None, **kwargs):
    """Retrieve the aggregate daily download time series by operating system"""
    endpoint = f"packages/{package}/system"
    params = _paramify("os", os)
    return pypi_stats_api(endpoint, params, **kwargs)
