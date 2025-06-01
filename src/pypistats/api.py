# src/pypistats/api.py
from __future__ import annotations

import datetime as dt
import json
import warnings # Keep for pypi_stats_api logic
from pathlib import Path

import cattrs # Keep for pypi_stats_api logic
import httpx # Keep for pypi_stats_api logic
from platformdirs import user_cache_dir
from slugify import slugify
import structlog # Keep for pypi_stats_api logic

# Assuming models are in .models - adjust if they are moved/renamed
from .models import DownloadStatistic, RecentAPIData, OverallPackageStats, RecentPackageStats

# Constants moved from __init__.py
# If __version__ is needed for USER_AGENT, it must be passed or imported carefully to avoid circularity.
# For now, let's assume USER_AGENT can be simplified or __version__ handled by the caller in __init__.py
# For simplicity here, let's define a simpler USER_AGENT in api.py or make it configurable.
# Alternatively, pypi_stats_api could take user_agent as a parameter.
# Let's try importing __version__ from _version directly if it's safe.
try:
    from ._version import __version__
except ImportError: # Fallback if _version.py is not generated yet or in a different place
    __version__ = "0.0.0-dev"

BASE_URL = "https://pypistats.org/api/"
CACHE_DIR = Path(user_cache_dir("pypistats"))
USER_AGENT = f"pypistats/{__version__}" # Needs __version__

logger = structlog.get_logger(__name__) # Each module should get its own logger

# --- Caching Logic ---
def _cache_filename(url: str) -> Path:
    """yyyy-mm-dd-url-slug.json"""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    slug = slugify(url)
    filename = CACHE_DIR / f"{today}-{slug}.json"
    return filename

def _load_cache(cache_file: Path) -> dict:
    if not cache_file.exists():
        return {}
    try:
        with cache_file.open("r") as f:
            data = json.load(f)
    except json.decoder.JSONDecodeError:
        logger.warn("Failed to decode cache file.", path=str(cache_file))
        return {}
    except OSError as e:
        logger.warn("Failed to read cache file.", path=str(cache_file), error=str(e))
        return {}
    return data

def _save_cache(cache_file: Path, data: dict) -> None:
    try:
        if not CACHE_DIR.exists():
            CACHE_DIR.mkdir(parents=True, exist_ok=True) # exist_ok=True for safety
        with cache_file.open("w") as f:
            json.dump(data, f)
    except OSError as e:
        logger.error("Failed to save cache.", path=str(cache_file), error=str(e))

def _clear_cache() -> None:
    """Delete old cache files. Meant to be registered with atexit."""
    if not CACHE_DIR.exists(): # Don't try to glob if dir doesn't exist
        return

    cache_files = CACHE_DIR.glob("**/*.json")
    this_month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    deleted_count = 0
    for cache_file in cache_files:
        if not cache_file.name.startswith(this_month):
            try:
                cache_file.unlink()
                deleted_count += 1
            except OSError as e:
                logger.warn("Failed to delete old cache file.", path=str(cache_file), error=str(e))
    if deleted_count > 0:
        logger.info(f"Cleared {deleted_count} old cache files.")

# --- Parameter Helper ---
def _paramify(param_name: str, param_value: bool | float | str | None) -> str:
    """If param_value, return &param_name=param_value"""
    if param_value is None:
        return ""

    val_str = str(param_value)
    if isinstance(param_value, bool):
        val_str = val_str.lower()

    return "&" + param_name + "=" + val_str

# --- Core API Function ---
def pypi_stats_api(
    endpoint: str,
    params: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    verbose: bool = False,
) -> OverallPackageStats | RecentPackageStats | str:
    """
    Calls the PyPI Stats API, handles caching, and returns structured data or an error string.
    """
    if params:
        if not params.startswith("?"):
             params = "?" + params
    else:
        params = ""

    url = BASE_URL + endpoint.lower() + params
    cache_file = _cache_filename(url)

    if verbose: logger.debug("API request", url=url, cache_file=str(cache_file))

    raw_api_response_dict = {}
    if cache_file.is_file():
        if verbose: logger.debug("Cache file exists, loading.", cache_file_path=str(cache_file))
        raw_api_response_dict = _load_cache(cache_file)
        if not raw_api_response_dict and verbose:
            logger.debug("Cache was empty or invalid.")

    if not raw_api_response_dict:
        if verbose: logger.debug("Fetching from network.", url=url)

        try:
            # Ensure httpx is imported where it's used, or at module level if always needed.
            # For this function, it's only needed if not cached.
            # import httpx # Already at module level
            response = httpx.get(url, headers={"User-Agent": USER_AGENT})
            if verbose: logger.debug("HTTP status code received.", status_code=response.status_code, url=url)
            response.raise_for_status()
            raw_api_response_dict = response.json()
            _save_cache(cache_file, raw_api_response_dict)
        except httpx.HTTPStatusError as e:
            logger.error("HTTP error during API request.", url=url, status_code=e.response.status_code, error=str(e))
            return f"HTTP error {e.response.status_code} for {url}"
        except httpx.RequestError as e:
            logger.error("Request error during API request.", url=url, error=str(e))
            return f"Request error for {url}: {str(e)}"
        except json.JSONDecodeError as e:
            logger.error("Failed to decode JSON response from API.", url=url, error=str(e))
            return f"Invalid JSON response from {url}"

    if not raw_api_response_dict or not raw_api_response_dict.get("data"):
        package_name_for_msg = raw_api_response_dict.get("package", "") if raw_api_response_dict else ""
        message = f"No data found for https://pypi.org/project/{package_name_for_msg}/"
        logger.warn(message, package_name=package_name_for_msg, api_url=url)
        return message

    converter = cattrs.Converter()
    structured_response: OverallPackageStats | RecentPackageStats
    is_recent_endpoint = "recent" in endpoint.lower()

    try:
        if is_recent_endpoint:
            structured_response = converter.structure(raw_api_response_dict, RecentPackageStats)
        else:
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

    return structured_response
