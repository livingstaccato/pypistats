from __future__ import annotations
from attrs import define, field

@define
class DownloadStatistic:
    """
    Represents a single download statistic entry, typically part of a list
    in API responses like 'overall', 'python_major', etc.
    """
    category: str
    date: str  # Dates are strings like "YYYY-MM-DD" or "YYYY-MM"
    downloads: int
    percent: str | None = field(default=None, kw_only=True) # For calculated percentages

@define
class RecentAPIData:
    """
    Represents the structure of the 'data' field from the /recent API endpoint.
    """
    last_day: int
    last_week: int | None = None # Optional, API varies based on 'period'
    last_month: int | None = None # Optional

@define
class BasePackageAPIResponse: # Common fields
    package: str
    type: str # e.g., "overall_downloads", "recent_downloads"

@define
class OverallPackageStats(BasePackageAPIResponse):
    """
    Represents the structured API response for 'overall', 'python_major', 'python_minor', 'system'.
    """
    data: list[DownloadStatistic]

@define
class RecentPackageStats(BasePackageAPIResponse):
    """
    Represents the structured API response for the 'recent' endpoint.
    """
    data: RecentAPIData
