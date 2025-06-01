from __future__ import annotations # Keep for modern typing

import datetime as dt
import re # Keep, it was used by original _python_major/minor_version, might be useful for callbacks

import click
from dateutil.relativedelta import relativedelta

import pypistats
# Assuming src/pypistats/_version.py exists and defines __version__
# If _version is in the same directory, it should be:
# from ._version import __version__ as pypistats_version
# If pypistats.__version__ is the canonical one:
from pypistats import __version__ as pypistats_version

# --- Custom Click Parameter Types and Callbacks ---

# Keep existing _month_name_to_yyyy_mm, ensure it raises ValueError on parse failure.
def _month_name_to_yyyy_mm(date_string: str, date_format: str) -> str:
    today = dt.date.today()
    parsed_date: dt.date | None = None
    # Try current year
    try:
        current_year_date = dt.datetime.strptime(
            f"{date_string} {today.year}", f"{date_format} %Y"
        ).date()
        if current_year_date <= today:
            parsed_date = current_year_date
        else: # Parsed date is in the future for the current year, try previous year
            pass # Fall through to try previous year
    except ValueError:
        pass # Fall through to try previous year

    # Try previous year if current year failed or resulted in a future date
    if parsed_date is None:
        try:
            parsed_date = dt.datetime.strptime(
                f"{date_string} {today.year-1}", f"{date_format} %Y"
            ).date()
        except ValueError as e:
            # If both fail, raise the error
            raise ValueError(f"Could not parse month '{date_string}' for current or previous year.") from e

    return parsed_date.isoformat()[:7] # Return YYYY-MM

class YYYYMMDDOptionalParamType(click.ParamType):
    name = "yyyy-mm[-dd]|name"

    def convert(self, value, param, ctx):
        if value is None: return None
        val_str = str(value)
        try: # yyyy-mm-dd
            dt.datetime.strptime(val_str, "%Y-%m-%d")
            return val_str
        except ValueError:
            try: # yyyy-mm or month name
                # Try to convert month name (e.g., 'jan') to 'YYYY-MM'
                processed_val = val_str
                try:
                    processed_val = _month_name_to_yyyy_mm(val_str, "%b") # Short month name
                except ValueError:
                    try:
                        processed_val = _month_name_to_yyyy_mm(val_str, "%B") # Full month name
                    except ValueError:
                        # Not a month name, assume it's already yyyy-mm or invalid
                        pass

                dt.datetime.strptime(processed_val, "%Y-%m") # Validate as YYYY-MM
                return processed_val
            except ValueError:
                self.fail(f"'{val_str}' is not a valid yyyy-mm-dd, yyyy-mm, or recognized month name.", param, ctx)

YYYY_MM_DD_OPTIONAL = YYYYMMDDOptionalParamType()

class YYYYMMParamType(click.ParamType):
    name = "yyyy-mm|name"
    def convert(self, value, param, ctx):
        if value is None: return None
        val_str = str(value)
        try:
            processed_val = val_str
            try:
                processed_val = _month_name_to_yyyy_mm(val_str, "%b")
            except ValueError:
                try:
                    processed_val = _month_name_to_yyyy_mm(val_str, "%B")
                except ValueError:
                    pass
            dt.datetime.strptime(processed_val, "%Y-%m")
            return processed_val
        except ValueError:
            self.fail(f"'{val_str}' is not a valid yyyy-mm format or a recognized month name.", param, ctx)
YYYY_MM = YYYYMMParamType()

def validate_python_major(ctx, param, value):
    if value is None: return None
    if not re.fullmatch(r"\d+", str(value)): # Use re.fullmatch for stricter match
        raise click.BadParameter("must be an integer (e.g., '3').")
    return str(value)

def validate_python_minor(ctx, param, value):
    if value is None: return None
    if not re.fullmatch(r"\d+\.\d+", str(value)): # Use re.fullmatch
        raise click.BadParameter("must be in X.Y format (e.g., '3.10').")
    return str(value)

# Keep date calculation helpers: _month, _last_month, _this_month
def _month(yyyy_mm_str: str) -> tuple[str, str]:
    year, month_val = map(int, yyyy_mm_str.split("-"))
    first = dt.date(year, month_val, 1)
    last = first + relativedelta(months=1) - relativedelta(days=1)
    return str(first), str(last)

def _last_month() -> tuple[str, str]:
    today = dt.date.today()
    d = today - relativedelta(months=1)
    return _month(d.isoformat()[:7])

def _this_month() -> str: # Returns start_date of current month
    today = dt.date.today()
    return str(dt.date(today.year, today.month, 1))

# --- Click CLI structure ---
@click.group(context_settings=dict(help_option_names=['-h', '--help']))
@click.version_option(pypistats_version, "-V", "--version", prog_name="pypistats", message="%(prog)s %(version)s")
@click.option("-v", "--verbose", is_flag=True, help="Print debug messages.")
@click.option(
    "--color", # Was -c, changed to --color
    type=click.Choice(("yes", "no", "auto"), case_sensitive=False),
    default="auto",
    show_default=True,
    help="Color terminal output.",
)
@click.pass_context
def cli(ctx, verbose: bool, color: str):
    """Python interface to PyPI Stats API https://pypistats.org/api"""
    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ctx.obj['color'] = color
    # Setup structlog level based on verbosity
    # This requires access to the logger configuration, which might be better done
    # once in __init__.py or a shared config module if verbosity needs to affect all loggers.
    # For now, verbose is passed to API calls.

# Helper for date processing common to multiple commands
def process_date_inputs(start_date_in, end_date_in, month_in, last_month_flag, this_month_flag):
    # This function takes the string inputs from Click options (which can be yyyy-mm or yyyy-mm-dd)
    # and resolves them to specific yyyy-mm-dd start and end dates for the API.
    s_date, e_date = start_date_in, end_date_in

    if month_in: # User specified --month yyyy-mm (already validated by YYYY_MM type)
        s_date, e_date = _month(month_in)
    elif last_month_flag:
        s_date, e_date = _last_month()
    elif this_month_flag:
        s_date = _this_month() # yyyy-mm-dd
        e_date = None # API handles None as "up to most recent"

    # If s_date/e_date are yyyy-mm (because user typed them or from YYYY_MM_DD_OPTIONAL), expand them.
    if s_date and len(s_date) == 7: # YYYY-MM
         s_date, temp_e_date_ignored = _month(s_date)
    if e_date and len(e_date) == 7: # YYYY-MM
        temp_s_date_ignored, e_date = _month(e_date)

    return s_date, e_date

# Shared options decorators
common_format_option = click.option(
    "-f", "--format",
    type=click.Choice(
        ("html", "json", "pretty", "md", "markdown", "rst", "tsv", "plot"), # "plot" is already here
        case_sensitive=False
    ),
    default="pretty",
    show_default=True,
    help="Output format."
)

def common_date_options(f):
    f = click.option("-sd", "--start-date", "start_date_in", type=YYYY_MM_DD_OPTIONAL, help="Start date (yyyy-mm[-dd] or month name).")(f)
    f = click.option("-ed", "--end-date", "end_date_in", type=YYYY_MM_DD_OPTIONAL, help="End date (yyyy-mm[-dd] or month name).")(f)
    f = click.option("-m", "--month", "month_in", type=YYYY_MM, help="Shortcut for a specific month (yyyy-mm or month name).")(f)
    f = click.option("-l", "--last-month", is_flag=True, help="Shortcut for last month.")(f)
    f = click.option("-t", "--this-month", is_flag=True, help="Shortcut for this month (data up to yesterday).")(f)
    f = click.option("-d", "--daily", is_flag=True, help="Show daily downloads (if applicable).")(f)
    f = click.option("--monthly", is_flag=True, help="Show monthly downloads (if applicable).")(f)
    return f

# --- Commands ---
@cli.command()
@click.argument("package")
@click.option("-p", "--period", type=click.Choice(("day", "week", "month"), case_sensitive=False), help="Period for recent stats.")
@common_format_option
@click.pass_context
def recent(ctx, package: str, period: str | None, format: str):
    """Aggregate downloads for the last day, week, or month."""
    output = pypistats.recent(
        package=package,
        period=period,
        format=format,
        verbose=ctx.obj['verbose']
        # color is handled by rich/output stage, not typically passed to core logic
    )
    click.echo(output) # Output will be handled by Rich later

@cli.command()
@click.argument("package")
@click.option(
    "--mirrors",
    type=click.Choice(("true", "false", "with", "without"), case_sensitive=False),
    help="Filter by mirror downloads. 'with/without' are for compatibility."
)
@common_date_options
@common_format_option
@click.pass_context
def overall(ctx, package: str, mirrors: str | None,
            start_date_in: str | None, end_date_in: str | None, month_in: str | None,
            last_month_flag: bool, this_month_flag: bool,
            daily: bool, monthly: bool, format: str):
    """Daily/monthly downloads over a period."""
    start_date, end_date = process_date_inputs(start_date_in, end_date_in, month_in, last_month_flag, this_month_flag)

    mirrors_param: bool | None = None
    if mirrors:
        if mirrors == "with": mirrors_param = True
        elif mirrors == "without": mirrors_param = False
        elif mirrors == "true": mirrors_param = True
        elif mirrors == "false": mirrors_param = False

    total_granularity = "daily" if daily else ("monthly" if monthly else "all")

    output = pypistats.overall(
        package=package,
        mirrors=mirrors_param,
        start_date=start_date,
        end_date=end_date,
        format=format,
        total=total_granularity,
        color=ctx.obj['color'], # For pypistats internal use if any, Rich will override for display
        verbose=ctx.obj['verbose'],
    )
    click.echo(output)

@cli.command("python-major") # Explicit command name
@click.argument("package")
@click.option("-V", "--version", "py_version", callback=validate_python_major, help="Python major version (e.g., '3').")
@common_date_options
@common_format_option
@click.pass_context
def python_major(ctx, package: str, py_version: str | None,
                 start_date_in: str | None, end_date_in: str | None, month_in: str | None,
                 last_month_flag: bool, this_month_flag: bool,
                 daily: bool, monthly: bool, format: str):
    """Daily/monthly downloads by Python major version."""
    start_date, end_date = process_date_inputs(start_date_in, end_date_in, month_in, last_month_flag, this_month_flag)
    total_granularity = "daily" if daily else ("monthly" if monthly else "all")
    output = pypistats.python_major(
        package=package,
        version=py_version,
        start_date=start_date,
        end_date=end_date,
        format=format,
        total=total_granularity,
        color=ctx.obj['color'],
        verbose=ctx.obj['verbose'],
    )
    click.echo(output)

@cli.command("python-minor") # Explicit command name
@click.argument("package")
@click.option("-V", "--version", "py_version", callback=validate_python_minor, help="Python minor version (e.g., '3.10').")
@common_date_options
@common_format_option
@click.pass_context
def python_minor(ctx, package: str, py_version: str | None,
                 start_date_in: str | None, end_date_in: str | None, month_in: str | None,
                 last_month_flag: bool, this_month_flag: bool,
                 daily: bool, monthly: bool, format: str):
    """Daily/monthly downloads by Python minor version."""
    start_date, end_date = process_date_inputs(start_date_in, end_date_in, month_in, last_month_flag, this_month_flag)
    total_granularity = "daily" if daily else ("monthly" if monthly else "all")
    output = pypistats.python_minor(
        package=package,
        version=py_version,
        start_date=start_date,
        end_date=end_date,
        format=format,
        total=total_granularity,
        color=ctx.obj['color'],
        verbose=ctx.obj['verbose'],
    )
    click.echo(output)

@cli.command()
@click.argument("package")
@click.option("-o", "--os", "operating_system", help="OS name (e.g., 'windows', 'linux', 'darwin', 'other').")
@common_date_options
@common_format_option
@click.pass_context
def system(ctx, package: str, operating_system: str | None,
           start_date_in: str | None, end_date_in: str | None, month_in: str | None,
           last_month_flag: bool, this_month_flag: bool,
           daily: bool, monthly: bool, format: str):
    """Daily/monthly downloads by Operating System."""
    start_date, end_date = process_date_inputs(start_date_in, end_date_in, month_in, last_month_flag, this_month_flag)
    total_granularity = "daily" if daily else ("monthly" if monthly else "all")
    output = pypistats.system(
        package=package,
        os=operating_system,
        start_date=start_date,
        end_date=end_date,
        format=format,
        total=total_granularity,
        color=ctx.obj['color'],
        verbose=ctx.obj['verbose'],
    )
    click.echo(output)

def main():
    # Call the click group. obj={} is a default if cli is called directly (e.g., in tests)
    # and not through the Click CLI runner which would normally manage the context.
    cli(obj={})

if __name__ == "__main__":
    main()
