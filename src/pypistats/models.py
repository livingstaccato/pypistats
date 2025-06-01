from __future__ import annotations

from attrs import define, field

@define
class DownloadStatistic:
    """
    Represents a single download statistic entry from the PyPI Stats API.
    Typically found in endpoints like 'overall', 'python_major', etc.
    """
    category: str
    date: str  # Dates are strings like "YYYY-MM-DD" or "YYYY-MM"
    downloads: int
    percent: str | None = field(default=None, kw_only=True) # Added field

@define
class RecentStats:
    """
    Represents download statistics for a recent period from the /recent endpoint.
    The 'category' here is usually the package name itself.
    """
    category: str  # Or perhaps package_name: str for clarity? API uses "category".
    last_day: int
    last_week: int
    last_month: int

@define
class PackageStats:
    """
    Represents the overall structure often returned by the API,
    containing package information and a list of data points.
    """
    package: str
    data: list[DownloadStatistic | RecentStats] # Using a union for flexibility
    # Depending on the endpoint, 'data' items could be DownloadStatistic or other types.
    # For 'recent', the 'data' list contains a single item that looks more like RecentStats
    # but is often a dictionary with 'category' (package name), 'last_day', etc.
    # Let's refine this if direct usage shows issues.

# We might need more specific container types later, e.g., for how 'recent'
# structures its full response if it differs significantly at the top level.
# The API returns a dict with 'package', 'type', 'data' (list of dicts)
# For 'recent', data is a list containing ONE dict:
# {'category': 'pypistats', 'last_day': 123, 'last_month': 4567, 'last_week': 890}
# So, perhaps RecentStats is the structure within the list for the 'recent' endpoint.
