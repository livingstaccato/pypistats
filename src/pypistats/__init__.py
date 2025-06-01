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
from .models import DownloadStatistic, RecentStats, PackageStats
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

    # Attempt to structure the raw dictionary into our attrs models
    try:
        # PackageStats is the top-level model. cattrs will use type hints
        # on PackageStats.data (which is list[DownloadStatistic | RecentStats])
        # to structure the items within the 'data' list.
        structured_package_stats = converter.structure(raw_api_response_dict, PackageStats)
    except Exception as e:
        package_name_for_error = raw_api_response_dict.get("package", "unknown package")
        logger.error(
            "Failed to structure API response with cattrs.",
            package_name=package_name_for_error,
            error=str(e),
            response_preview="".join(str(raw_api_response_dict)[:200].splitlines()), # Log a preview, remove newlines
            api_url=url
        )
        # Mimic returning a string on error, similar to original behavior.
        return f"Error processing API data for {package_name_for_error}: {str(e)}"

    # Actual first and last dates of the fetched data
    first, last = _date_range(structured_package_stats.data)

    # Validate end date
    if end_date and first and end_date < first: # 'first' is from the new _date_range
        msg = (
            f"Requested end date ({end_date}) is before earliest available "
            f"data ({first}), because data is only available for 180 days. "
            "See https://pypistats.org/about#data"
        )
        raise ValueError(msg) # Or log error and return message

    # Validate start date
    if start_date and first and start_date < first:
        # Original used warnings.warn - keep this for now
        warnings.warn(
            f"Requested start date ({start_date}) is before earliest available "
            f"data ({first}), because data is only available for 180 days. "
            "See https://pypistats.org/about#data",
            stacklevel=3, # Keep original stacklevel if it was important
        )

    # TODO: The following lines need to be updated to work with structured_package_stats.data
    # For now, they will likely cause errors or not work as expected.
    if start_date or end_date:
        structured_package_stats_data = _filter(structured_package_stats.data, start_date, end_date)
        structured_package_stats.data = structured_package_stats_data

    if start_date:
        first = start_date
    if end_date:
        last = end_date

    # The data to be processed by total, sort, percent, grand_total, etc.
    # This will be a list of DownloadStatistic or RecentStats objects.
    data_to_process = structured_package_stats.data

    if total == "monthly":
        # Filter for DownloadStatistic items before processing
        stats_data = [s for s in data_to_process if isinstance(s, DownloadStatistic)]
        data_to_process = _monthly_total(stats_data)
    elif total == "all":
        stats_data = [s for s in data_to_process if isinstance(s, DownloadStatistic)]
        data_to_process = _total(stats_data)

    # Update structured_package_stats.data with the processed data if necessary
    # This is important if _monthly_total or _total change the list content/type
    structured_package_stats.data = data_to_process


    if format == "json":
        # If totals were applied, structured_package_stats.data might now be List[DownloadStatistic]
        # even if original was List[RecentStats], ensure converter can handle this.
        return json.dumps(converter.unstructure(structured_package_stats))

    # Data for tabulation is what we've processed so far
    data_for_tabulation = data_to_process
    if sort:
        data_for_tabulation = _sort(data_for_tabulation)

    # _percent and _grand_total expect list[DownloadStatistic]
    # and modify/add to it.
    # We need to ensure data_for_tabulation is of the correct type.
    # If it was RecentStats and went through _total, it's now DownloadStatistic.
    # If it was RecentStats and didn't go through _total, these might not apply or error.
    if data_for_tabulation and isinstance(data_for_tabulation[0], DownloadStatistic):
        # Cast to list[DownloadStatistic] for type checker, assuming homogeneity after processing
        download_stats_for_tabulation = [ds for ds in data_for_tabulation if isinstance(ds, DownloadStatistic)]
        download_stats_for_tabulation = _percent(download_stats_for_tabulation)
        download_stats_for_tabulation = _grand_total(download_stats_for_tabulation)
        data_for_tabulation = download_stats_for_tabulation
    elif data_for_tabulation and isinstance(data_for_tabulation[0], RecentStats):
        # _percent and _grand_total are not designed for RecentStats.
        # Log or handle this case: maybe they shouldn't be called for RecentStats.
        logger.info("Skipping percent and grand_total for RecentStats data type.")

    # 'color_param' is the color choice ('yes', 'no', 'auto') from CLI.

    # The variable 'data_for_tabulation' holds the list of attrs objects.
    # 'format' is the requested output format string.

    if format is None: # Should not happen with Click default
        return data_for_tabulation

    output: str = "" # Initialize output

    if format == "pretty":
        force_terminal: bool | None = None
        if color_param == "yes":
            force_terminal = True
        elif color_param == "no":
            force_terminal = False

        console = Console(force_terminal=force_terminal)
        _tabulate_rich_pretty(data_for_tabulation, structured_package_stats.package, console)
        return "" # Rich prints directly, return empty string

    elif format in ("html", "markdown", "rst", "tsv"): # md is alias for markdown
        if not data_for_tabulation:
            return "No data to tabulate."

        # Ensure converter is available
        local_converter = cattrs.Converter() # Re-initialize if not passed or available in wider scope
        try:
            data_as_dicts = [local_converter.unstructure(item) for item in data_for_tabulation]
        except Exception as e:
            logger.error("Failed to unstructure data for pytablewriter", error=str(e))
            return "Error preparing data for tabulation."

        headers: list[str] = []
        if data_as_dicts: # Ensure there's data before accessing first item
            first_item_dict = data_as_dicts[0]
            # Check the type of the original attrs object for correct header determination
            original_first_item = data_for_tabulation[0]
            if isinstance(original_first_item, DownloadStatistic):
                headers = ["category", "date", "downloads"]
                if "percent" in first_item_dict and first_item_dict["percent"] is not None:
                    headers.append("percent")
            elif isinstance(original_first_item, RecentStats):
                headers = ["category", "last_day", "last_week", "last_month"]

        # Use _pytablewriter for these formats
        output = _pytablewriter(headers, data_as_dicts, format) # format is md, rst etc.

    elif format == "plot":
        force_terminal: bool | None = None
        if color_param == "yes": force_terminal = True
        elif color_param == "no": force_terminal = False
        plot_console = Console(force_terminal=force_terminal)

        download_stats_for_plot = [item for item in data_for_tabulation if isinstance(item, DownloadStatistic)]

        if not download_stats_for_plot:
            plot_console.print("[yellow]Warning: No data suitable for plotting (expected DownloadStatistic items).[/yellow]")
            return "No data suitable for plotting."

        _generate_plotext_plot(download_stats_for_plot, structured_package_stats.package, plot_console)
        return "" # plotext prints directly

    else:
        logger.warn("Unknown or unhandled format requested for tabulation", requested_format=format)
        return f"Format '{format}' not supported by this path."

    # Add date range footer, similar to original logic
    if first and format not in ["numpy", "pandas", "pretty", "plot"]: # "pretty" & "plot" are handled directly
        return f"{output}\nDate range: {first} - {last}\n"
    else:
        return output # For md, rst, tsv, html etc.


def _generate_plotext_plot(
    data_list: list[DownloadStatistic], # Plotting is typically for DownloadStatistic
    package_name: str,
    console: Console # Rich console for printing any messages/errors around plotting
) -> None: # plotext prints directly
    """Generates and displays a terminal plot using plotext."""

    if not data_list:
        console.print(f"No data to plot for {package_name}.")
        return

    # Filter for DownloadStatistic instances and ensure they have valid dates and downloads
    plot_data = []
    for item in data_list:
        if isinstance(item, DownloadStatistic) and hasattr(item, 'date') and isinstance(item.downloads, int):
            try:
                plot_data.append({'date': item.date, 'downloads': item.downloads, 'category': item.category})
            except (ValueError, TypeError):
                logger.warn("Skipping item with invalid date for plotting.", date_value=getattr(item, 'date', 'N/A'))
                continue

    if not plot_data:
        console.print(f"No suitable data points found for plotting for {package_name}.")
        return

    # Sort data by date for time-series plotting
    try:
        plot_data.sort(key=lambda x: x['date'])
    except TypeError as e:
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
    downloads_list = [item['downloads'] for item in plot_data] # Renamed to avoid conflict

    plotext.clear_figure()
    plotext.plot_date(dates_str, downloads_list)

    plotext.title(plot_title)
    plotext.xlabel("Date")
    plotext.ylabel("Downloads")

    plotext.show()

def _filter(
    data: list[DownloadStatistic | RecentStats],
    start_date: str | None = None,
    end_date: str | None = None
) -> list[DownloadStatistic | RecentStats]:
    """Only return data with dates between start_date and end_date."""
    # This function primarily applies to DownloadStatistic objects which have a 'date'
    filtered_data: list[DownloadStatistic | RecentStats] = []

    current_data = data # Start with the original list

    if start_date:
        processed_items: list[DownloadStatistic | RecentStats] = []
        for item in current_data:
            # Check if item is DownloadStatistic and has a date >= start_date
            if isinstance(item, DownloadStatistic) and item.date >= start_date:
                processed_items.append(item)
            elif not isinstance(item, DownloadStatistic): # Keep non-DownloadStatistic items
                processed_items.append(item)
        current_data = processed_items # Update data to be the filtered list for the next step

    if end_date:
        processed_items: list[DownloadStatistic | RecentStats] = []
        for item in current_data: # data is now potentially filtered by start_date
            # Check if item is DownloadStatistic and has a date <= end_date
            if isinstance(item, DownloadStatistic) and item.date <= end_date:
                processed_items.append(item)
            elif not isinstance(item, DownloadStatistic): # Keep non-DownloadStatistic items
                processed_items.append(item)
        current_data = processed_items # Update data with end_date filtering

    return current_data


def _sort(data: list[DownloadStatistic | RecentStats]) -> list[DownloadStatistic | RecentStats]:
    """Sort by downloads. Handles items with 'downloads' or 'last_day' (for RecentStats)."""

    if not data:
        return []

    # Determine sort key based on type of first element (assuming homogeneous list for sorting purposes)
    # or check attributes.
    if isinstance(data[0], DownloadStatistic):
        # Sort DownloadStatistic items by 'downloads'
        return sorted(data, key=lambda item: item.downloads if isinstance(item, DownloadStatistic) else 0, reverse=True)
    elif isinstance(data[0], RecentStats):
        # For RecentStats, 'last_day' might be a good proxy for "most recent downloads"
        # The original API for 'recent' doesn't really have a 'downloads' field in the same way.
        # Original _sort was generic for dicts with 'downloads'.
        # Let's sort RecentStats by 'last_day' as a sensible default if sorting is applied.
        return sorted(data, key=lambda item: item.last_day if isinstance(item, RecentStats) else 0, reverse=True)

    # If it's a mixed list or unknown type, return as is or log a warning.
    # For now, returning as is if type is not recognized for sorting.
    logger.warn("Attempted to sort a list of unknown or mixed item types.", first_item_type=type(data[0]))
    return data


def _monthly_total(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
    """Sum all downloads per category, by month, for DownloadStatistic items."""
    totalled: dict[str, dict[str, int]] = {} # category -> {month_str -> total_downloads}
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
        new_data.append(DownloadStatistic(category=category, date="aggregated", downloads=downloads))


    return new_data


def _date_range(data: list[DownloadStatistic | RecentStats]) -> tuple[str | None, str | None]:
    """Return the first and last dates in data if items have a 'date' attribute."""
    # Filter out items that don't have a 'date' attribute (like RecentStats)
    # or where 'date' might be None/empty string. Assumes 'date' is str if it exists.
    dates = [item.date for item in data if hasattr(item, 'date') and isinstance(getattr(item, 'date', None), str) and getattr(item, 'date', None)]

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
    new_row = DownloadStatistic(category="Total", date="aggregated", downloads=grand_total)

    # Return a new list with the total row appended
    return data + [new_row]


def _percent(data: list[DownloadStatistic]) -> list[DownloadStatistic]:
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
    data_list: list[DownloadStatistic | RecentStats],
    package_name: str, # For context if needed in title or header
    console: Console # Pass a Rich Console configured with color system
) -> None: # This function will print directly to the console
    """Generates and prints a 'pretty' table using rich.table.Table."""

    if not data_list:
        console.print(f"No data to display for {package_name}.")
        return

    table = Table(title=f"Stats for {package_name}", show_lines=True)

    # Determine headers from the type of the first item
    # This assumes a list of homogeneous items, or that they share common fields for columns.
    first_item = data_list[0]
    headers: list[str] = []

    if isinstance(first_item, DownloadStatistic):
        # Order matters for display
        headers = ["category", "date", "downloads"]
        if hasattr(first_item, 'percent') and first_item.percent is not None:
            headers.append("percent")
    elif isinstance(first_item, RecentStats):
        headers = ["category", "last_day", "last_week", "last_month"]
    else:
        console.print("[red]Error: Unknown data type for table display.[/red]")
        return

    for header in headers:
        table.add_column(header.replace("_", " ").title(), justify="right" if header not in ["category", "date"] else "left")

    for item in data_list:
        row_values: list[str | Text] = []
        for header in headers:
            value = getattr(item, header, "")

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
