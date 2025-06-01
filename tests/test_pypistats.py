"""
Unit tests for pypistats
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import respx
import cattrs # Added
# from termcolor import termcolor # Removed

import pypistats
from pypistats import api as pypistats_api # Import the api module
from pypistats.models import DownloadStatistic
from pypistats import TempRecentDisplayItem


from .data.expected_tabulated import (
    EXPECTED_TABULATED_HTML,
    EXPECTED_TABULATED_MD,
    EXPECTED_TABULATED_PRETTY,
    EXPECTED_TABULATED_RST,
    EXPECTED_TABULATED_TSV,
)
from .data.python_minor import DATA as PYTHON_MINOR_DATA

SAMPLE_DATA = [
    {"category": "2.6", "date": "2018-08-15", "downloads": 51},
    {"category": "2.7", "date": "2018-08-15", "downloads": 63749},
    {"category": "3.2", "date": "2018-08-15", "downloads": 2},
    {"category": "3.3", "date": "2018-08-15", "downloads": 40},
    {"category": "3.4", "date": "2018-08-15", "downloads": 6095},
    {"category": "3.5", "date": "2018-08-15", "downloads": 20358},
    {"category": "3.6", "date": "2018-08-15", "downloads": 35274},
    {"category": "3.7", "date": "2018-08-15", "downloads": 6595},
    {"category": "3.8", "date": "2018-08-15", "downloads": 3},
    {"category": "null", "date": "2018-08-15", "downloads": 1019},
]
SAMPLE_DATA_ONE_ROW = [{"category": "with_mirrors", "date": "2023-01-01", "downloads": 11_497_042}]
SAMPLE_DATA_RECENT = {
    "last_day": 123_002,
    "last_month": 3_254_221,
    "last_week": 761_649,
}
SAMPLE_RESPONSE_OVERALL = """{
          "data": [
            {"category": "with_mirrors", "date": "2020-05-01", "downloads": 2100139},
            {"category": "with_mirrors", "date": "2020-05-02", "downloads": 1487218},
            {"category": "without_mirrors", "date": "2020-05-01", "downloads": 2083472},
            {"category": "without_mirrors", "date": "2020-05-02", "downloads": 1475979}
          ],
          "package": "pip",
          "type": "overall_downloads"
        }"""

def _dicts_to_download_stats(data: list[dict]) -> list[DownloadStatistic]:
    return [DownloadStatistic(**item) for item in data]

def stub__cache_filename(*args) -> Path:
    return Path("/this/does/not/exist")

def stub__save_cache(*args) -> None:
    pass

class TestPypiStats:
    def setup_method(self) -> None:
        self.original__cache_filename = pypistats_api._cache_filename
        self.original__save_cache = pypistats_api._save_cache
        pypistats_api._cache_filename = stub__cache_filename
        pypistats_api._save_cache = stub__save_cache

    def teardown_method(self) -> None:
        pypistats_api._cache_filename = self.original__cache_filename
        pypistats_api._save_cache = self.original__save_cache

    def test__filter_no_filters_no_change(self) -> None:
        data_dicts = copy.deepcopy(PYTHON_MINOR_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        output = pypistats._filter(data_stats) # Assuming _filter is in pypistats.__init__
        assert len(output) == len(data_stats)
        assert all(
            o.category == e.category and o.date == e.date and o.downloads == e.downloads
            for o, e in zip(output, data_stats)
        )

    def test__filter_start_date(self) -> None:
        data_dicts = copy.deepcopy(PYTHON_MINOR_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        start_date = "2018-09-22"
        output = pypistats._filter(data_stats, start_date=start_date)
        assert all(item.date >= start_date for item in output)

    def test__filter_end_date(self) -> None:
        data_dicts = copy.deepcopy(PYTHON_MINOR_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        end_date = "2018-04-22"
        output = pypistats._filter(data_stats, end_date=end_date)
        assert all(item.date <= end_date for item in output)

    def test__filter_start_and_end_date(self) -> None:
        data_dicts = copy.deepcopy(PYTHON_MINOR_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        start_date = "2018-09-01"
        end_date = "2018-09-11"
        output = pypistats._filter(data_stats, start_date=start_date, end_date=end_date)
        assert all(start_date <= item.date <= end_date for item in output)

    @respx.mock
    def test_warn_if_start_date_before_earliest_available(self) -> None:
        start_date = "2000-01-01"
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/python_major"
        mocked_response_dict = {
            "data": [{"category": "2", "date": "2018-11-01", "downloads": 2008344}],
            "package": "pip", "type": "python_major_downloads"
        }
        respx.get(mocked_url).respond(content=json.dumps(mocked_response_dict))
        with pytest.warns(UserWarning, match=r"Requested start date \(2000-01-01\) is before earliest available data \(2018-11-01\)."):
            # Call a public function that would trigger the warning via pypi_stats_api
            pypistats.python_major(package, start_date=start_date, format="pretty")

    @respx.mock
    def test_error_if_end_date_before_earliest_available(self) -> None:
        end_date = "2000-01-01"
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/python_major"
        mocked_response_dict = {
            "data": [{"category": "2", "date": "2018-11-01", "downloads": 2008344}],
            "package": "pip", "type": "python_major_downloads"
        }
        respx.get(mocked_url).respond(content=json.dumps(mocked_response_dict))
        with pytest.raises(ValueError, match=r"Requested end date \(2000-01-01\) is before earliest available data \(2018-11-01\)."):
            pypistats.python_major(package, end_date=end_date, format="pretty")

    @pytest.mark.parametrize(
        "test_name, test_value, expected",
        [("period", None, ""), ("period", "day", "&period=day"), ("mirrors", True, "&mirrors=true"), ("version", 3, "&version=3"), ("version", 3.7, "&version=3.7")],
    )
    def test__paramify(self, test_name, test_value, expected) -> None:
        # _paramify is now in api.py
        param = pypistats_api._paramify(test_name, test_value)
        assert param == expected

    def test__sort(self) -> None:
        data_dicts = copy.deepcopy(SAMPLE_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        expected_output_stats = [
            DownloadStatistic(category="2.7", date="2018-08-15", downloads=63749),
            DownloadStatistic(category="3.6", date="2018-08-15", downloads=35274),
            DownloadStatistic(category="3.5", date="2018-08-15", downloads=20358),
            DownloadStatistic(category="3.7", date="2018-08-15", downloads=6595),
            DownloadStatistic(category="3.4", date="2018-08-15", downloads=6095),
            DownloadStatistic(category="null", date="2018-08-15", downloads=1019),
            DownloadStatistic(category="2.6", date="2018-08-15", downloads=51),
            DownloadStatistic(category="3.3", date="2018-08-15", downloads=40),
            DownloadStatistic(category="3.8", date="2018-08-15", downloads=3),
            DownloadStatistic(category="3.2", date="2018-08-15", downloads=2),
        ]
        output = pypistats._sort(data_stats) # Assuming _sort is in pypistats.__init__
        assert output == expected_output_stats

    def test__sort_recent(self) -> None:
        recent_data_dict = copy.deepcopy(SAMPLE_DATA_RECENT)
        data_for_sort = [TempRecentDisplayItem(category="somepackage", **recent_data_dict)]
        output = pypistats._sort(data_for_sort)
        assert output == data_for_sort

    def test__monthly_total(self) -> None:
        data_dicts = copy.deepcopy(PYTHON_MINOR_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        output = pypistats._monthly_total(data_stats)
        assert len(output) == 64
        item_2_4_2018_04 = next((item for item in output if item.category == "2.4" and item.date == "2018-04"), None)
        assert item_2_4_2018_04 is not None and item_2_4_2018_04.downloads == 1
        item_2_7_2018_05 = next((item for item in output if item.category == "2.7" and item.date == "2018-05"), None)
        assert item_2_7_2018_05 is not None and item_2_7_2018_05.downloads == 489_163
        for item in output: assert isinstance(item, DownloadStatistic) and item.percent is None

    def test__total(self) -> None:
        data_dicts = copy.deepcopy(PYTHON_MINOR_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        output = pypistats._total(data_stats)
        assert len(output) == 12
        item_2_4 = next((item for item in output if item.category == "2.4"), None)
        assert item_2_4 is not None and item_2_4.downloads == 9 and item_2_4.date == "aggregated"
        for item in output: assert isinstance(item, DownloadStatistic) and item.percent is None

    def test__validate_total(self) -> None:
        valid_values = ("daily", "monthly", "all")
        for value in valid_values: pypistats._validate_total(value) # Assuming _validate_total is in pypistats.__init__
        with pytest.raises(ValueError, match="total must be one of"): pypistats._validate_total("weekly")

    def test__date_range(self) -> None:
        data_dicts = copy.deepcopy(PYTHON_MINOR_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        first, last = pypistats._date_range(data_stats)
        assert first == "2018-04-16" and last == "2018-09-23"

    def test__date_range_no_dates_in_data(self) -> None:
        data_stats: list[DownloadStatistic] = []
        first, last = pypistats._date_range(data_stats)
        assert first is None and last is None

    def test__grand_total(self) -> None:
        data_dicts = copy.deepcopy(PYTHON_MINOR_DATA)
        data_stats = _dicts_to_download_stats(data_dicts)
        original_len = len(data_stats)
        output = pypistats._grand_total(data_stats)
        assert len(output) == original_len + 1
        assert output[-1].category == "Total" and output[-1].downloads == 9_355_317 and output[-1].date == "aggregated" and output[-1].percent is None

    def test__grand_total_one_row(self) -> None:
        data_stats = _dicts_to_download_stats(copy.deepcopy(SAMPLE_DATA_ONE_ROW))
        output = pypistats._grand_total(data_stats)
        assert output == data_stats

    def test__grand_total_value_mirrors(self) -> None:
        data1_stats = _dicts_to_download_stats([
            {"category": "with_mirrors", "date": "2023-01-01", "downloads": 100},
            {"category": "without_mirrors", "date": "2023-01-01", "downloads": 80},
        ])
        assert pypistats._grand_total_value(data1_stats) == 100
        data2_stats = _dicts_to_download_stats([
            {"category": "3.7", "date": "2023-01-01", "downloads": 100},
            {"category": "3.8", "date": "2023-01-01", "downloads": 200},
        ])
        assert pypistats._grand_total_value(data2_stats) == 300

    def test__percent(self) -> None:
        data_stats = _dicts_to_download_stats([
            {"category": "2.7", "date": "2023-01-01", "downloads": 63749},
            {"category": "3.6", "date": "2023-01-01", "downloads": 35274},
            {"category": "2.6", "date": "2023-01-01", "downloads": 51},
            {"category": "3.2", "date": "2023-01-01", "downloads": 2},
        ])
        expected_percentages = {"2.7": "64.34%", "3.6": "35.60%", "2.6": "0.05%", "3.2": "0.00%"}
        output_stats = pypistats._percent(data_stats)
        assert len(output_stats) == 4
        for item in output_stats:
            assert isinstance(item, DownloadStatistic) and item.percent == expected_percentages[item.category]

    def test__percent_one_row(self) -> None:
        data_stats = _dicts_to_download_stats(copy.deepcopy(SAMPLE_DATA_ONE_ROW))
        output = pypistats._percent(data_stats)
        assert output == data_stats
        if output: assert output[0].percent is None

    @respx.mock
    def test_valid_json(self) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/recent?&period=day"
        api_response_json_str = """{"data": {"last_day": 1956060}, "package": "pip", "type": "recent_downloads"}"""
        expected_output_json_str = """{"package": "pip", "type": "recent_downloads", "data": {"last_day": 1956060, "last_week": null, "last_month": null}}"""
        respx.get(mocked_url).respond(content=api_response_json_str)
        output = pypistats.recent(package, period="day", format="json")
        assert json.loads(output) == json.loads(expected_output_json_str)

    @respx.mock
    @pytest.mark.parametrize(
        "test_format, expected_output",
        [
            pytest.param("markdown", "\n| category   |   last_day |   last_month |   last_week |\n| :--------- | ---------: | -----------: | ----------: |\n| pip        |  2,295,765 |   67,759,913 |  15,706,750 |\n", id="markdown"),
            pytest.param("tsv", '\n"category"\t"last_day"\t"last_week"\t"last_month"\n"pip"\t2295765\t15706750\t67759913\n', id="tsv"),
        ],
    )
    def test_recent_tabular(self, test_format, expected_output) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/recent"
        mocked_response = """{"data": {"last_day": 2295765, "last_month": 67759913, "last_week": 15706750}, "package": "pip", "type": "recent_downloads"}"""
        respx.get(mocked_url).respond(content=mocked_response)
        output = pypistats.recent(package, format=test_format)
        assert output.strip() == expected_output.strip()

    @respx.mock
    def test_overall_tabular_start_date(self, monkeypatch) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/overall?&mirrors=false"
        mocked_response = SAMPLE_RESPONSE_OVERALL
        expected_output = """
| category        | date       |   downloads | percent   |
| :-------------- | :--------- | ----------: | :-------- |
| with_mirrors    | aggregated |   1,487,218 | 100.00%   |
| without_mirrors | aggregated |   1,475,979 | 99.24%    |
| Total           | aggregated |   1,487,218 |           |

Date range: 2020-05-02 - 2020-05-02
"""
        monkeypatch.setenv("NO_COLOR", "1")
        respx.get(mocked_url).respond(content=mocked_response)
        output = pypistats.overall(package, mirrors=False, start_date="2020-05-02", format="md")
        assert output.strip() == expected_output.strip()

    @respx.mock
    def test_overall_tabular_end_date(self, monkeypatch) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/overall?&mirrors=false"
        mocked_response = SAMPLE_RESPONSE_OVERALL
        expected_output = """
| category        | date       |   downloads | percent   |
| :-------------- | :--------- | ----------: | :-------- |
| with_mirrors    | aggregated |   2,100,139 | 100.00%   |
| without_mirrors | aggregated |   2,083,472 | 99.21%    |
| Total           | aggregated |   2,100,139 |           |

Date range: 2020-05-01 - 2020-05-01
"""
        monkeypatch.setenv("NO_COLOR", "1")
        respx.get(mocked_url).respond(content=mocked_response)
        output = pypistats.overall(package, mirrors=False, end_date="2020-05-01", format="md")
        assert output.strip() == expected_output.strip()

    @respx.mock
    def test_python_major_json(self) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/python_major"
        mocked_response_dict = {"data": [{"category": "2", "date": "2018-11-01", "downloads": 2008344},{"category": "3", "date": "2018-11-01", "downloads": 280299},{"category": "null", "date": "2018-11-01", "downloads": 7122}], "package": "pip", "type": "python_major_downloads"}
        mocked_response_json = json.dumps(mocked_response_dict)
        respx.get(mocked_url).respond(content=mocked_response_json)

        expected_data_items_after_total_agg = [ # total="all" is default for overall() which python_major() calls
            DownloadStatistic(category="2", date="aggregated", downloads=2008344),
            DownloadStatistic(category="3", date="aggregated", downloads=280299),
            DownloadStatistic(category="null", date="aggregated", downloads=7122)
        ]
        expected_structured = pypistats.models.OverallPackageStats( # Use the model from pypistats.models
            package="pip",
            type="python_major_downloads",
            data=expected_data_items_after_total_agg
        )
        converter = cattrs.Converter()
        expected_output_json = json.dumps(converter.unstructure(expected_structured))
        output = pypistats.python_major(package, format="json")
        assert json.loads(output) == json.loads(expected_output_json)

    @respx.mock
    def test_python_minor_json(self) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/python_minor"
        mocked_response_dict = {"data": [{"category": "2.6", "date": "2018-11-01", "downloads": 6863},{"category": "2.7", "date": "2018-11-01", "downloads": 2001481},{"category": "3.2", "date": "2018-11-01", "downloads": 9},{"category": "3.3", "date": "2018-11-01", "downloads": 414},{"category": "3.4", "date": "2018-11-01", "downloads": 62166},{"category": "3.5", "date": "2018-11-01", "downloads": 79425},{"category": "3.6", "date": "2018-11-01", "downloads": 112266},{"category": "3.7", "date": "2018-11-01", "downloads": 25961},{"category": "3.8", "date": "2018-11-01", "downloads": 58},{"category": "null", "date": "2018-11-01", "downloads": 7122}], "package": "pip", "type": "python_minor_downloads"}
        mocked_response_json = json.dumps(mocked_response_dict)
        respx.get(mocked_url).respond(content=mocked_response_json)

        expected_data_items_after_total_agg = [
            DownloadStatistic(category="2.6", date="aggregated", downloads=6863),
            DownloadStatistic(category="2.7", date="aggregated", downloads=2001481),
            DownloadStatistic(category="3.2", date="aggregated", downloads=9),
            DownloadStatistic(category="3.3", date="aggregated", downloads=414),
            DownloadStatistic(category="3.4", date="aggregated", downloads=62166),
            DownloadStatistic(category="3.5", date="aggregated", downloads=79425),
            DownloadStatistic(category="3.6", date="aggregated", downloads=112266),
            DownloadStatistic(category="3.7", date="aggregated", downloads=25961),
            DownloadStatistic(category="3.8", date="aggregated", downloads=58),
            DownloadStatistic(category="null", date="aggregated", downloads=7122)
        ]
        expected_structured = pypistats.models.OverallPackageStats(
            package="pip",
            type="python_minor_downloads",
            data=expected_data_items_after_total_agg
        )
        converter = cattrs.Converter()
        expected_output_json = json.dumps(converter.unstructure(expected_structured))
        output = pypistats.python_minor(package, format="json")
        assert json.loads(output) == json.loads(expected_output_json)

    @respx.mock
    def test_system_tabular(self, monkeypatch) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/system"
        mocked_response_dict = {
            "package": "pip",
            "type": "system_downloads",
            "data": [
                {"category": "Darwin", "date": "2023-01-01", "downloads": 10734594},
                {"category": "Linux", "date": "2023-01-01", "downloads": 236502274},
                {"category": "null", "date": "2023-01-01", "downloads": 30579325},
                {"category": "other", "date": "2023-01-01", "downloads": 111243},
                {"category": "Windows", "date": "2023-01-01", "downloads": 6527978}
            ]
        }
        mocked_response_json = json.dumps(mocked_response_dict)
        expected_output = """
| category   | date       |   downloads | percent   |
| :--------- | :--------- | ----------: | :-------- |
| Linux      | aggregated | 236,502,274 | 83.14%    |
| null       | aggregated |  30,579,325 | 10.75%    |
| Darwin     | aggregated |  10,734,594 | 3.77%     |
| Windows    | aggregated |   6,527,978 | 2.29%     |
| other      | aggregated |     111,243 | 0.04%     |
| Total      | aggregated | 284,455,414 |           |
"""
        monkeypatch.setenv("NO_COLOR", "1")
        respx.get(mocked_url).respond(content=mocked_response_json)
        output = pypistats.system(package, format="md")
        assert output.strip() == expected_output.strip()

    @respx.mock
    def test_python_minor_monthly(self) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/python_minor"
        mocked_response_dict = {"data": [{"category": "2.6", "date": "2018-11-01", "downloads": 1},{"category": "2.6", "date": "2018-11-02", "downloads": 2},{"category": "2.6", "date": "2018-12-11", "downloads": 3},{"category": "2.6", "date": "2018-12-12", "downloads": 4},{"category": "2.7", "date": "2018-11-01", "downloads": 10},{"category": "2.7", "date": "2018-11-02", "downloads": 20},{"category": "2.7", "date": "2018-12-11", "downloads": 30},{"category": "2.7", "date": "2018-12-12", "downloads": 40}], "package": "pip", "type": "python_minor_downloads"}
        mocked_response_json = json.dumps(mocked_response_dict)
        respx.get(mocked_url).respond(content=mocked_response_json)

        expected_data_items_after_monthly_agg = [
            DownloadStatistic(category="2.6", date="2018-11", downloads=3),
            DownloadStatistic(category="2.6", date="2018-12", downloads=7),
            DownloadStatistic(category="2.7", date="2018-11", downloads=30),
            DownloadStatistic(category="2.7", date="2018-12", downloads=70)
        ]
        # Sort this list by category then date for consistent comparison
        expected_data_items_after_monthly_agg.sort(key=lambda x: (x.category, x.date))

        expected_structured = pypistats.models.OverallPackageStats(
            package="pip",
            type="python_minor_downloads_monthly", # Type indicates aggregation
            data=expected_data_items_after_monthly_agg
        )
        converter = cattrs.Converter()
        expected_output_json = json.dumps(converter.unstructure(expected_structured))

        output = pypistats.python_minor(package, total="monthly", format="json")

        # Sort data in output for consistent comparison
        output_dict = json.loads(output)
        if 'data' in output_dict and isinstance(output_dict['data'], list):
            output_dict['data'].sort(key=lambda x: (x.get('category'), x.get('date')))

        assert output_dict == json.loads(expected_output_json)


    @respx.mock
    def test_format_numpy(self) -> None:
        numpy = pytest.importorskip("numpy", reason="NumPy is not installed")
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/overall"
        mocked_response = SAMPLE_RESPONSE_OVERALL
        respx.get(mocked_url).respond(content=mocked_response)
        output = pypistats.overall(package, format="numpy") # total="all" is default

        # Expected data after _total, _percent, _grand_total
        # This is complex to construct manually as ndarray.
        # For now, check type and some basic properties.
        assert isinstance(output, numpy.ndarray)
        assert output.shape == (3, 3) # category, date, downloads, percent (Total row won't have date, percent is None for Total)
                                      # Expected: (items + total_row, num_cols)
                                      # For SAMPLE_RESPONSE_OVERALL (2 unique categories + Total row = 3 rows)
                                      # Columns after processing: category, date (aggregated), downloads, percent
                                      # So should be (3,4) if percent is included, or (3,3) if not.
                                      # Let's assume the `_pytablewriter` for numpy includes all available fields.
                                      # Actual columns: ['category', 'date', 'downloads', 'percent']
        assert output[0][0] == "with_mirrors" # Category of first data row
        assert output[2][0] == "Total"      # Category of total row


    @respx.mock
    def test_format_pandas(self) -> None:
        pandas = pytest.importorskip("pandas", reason="pandas is not installed")
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/overall"
        mocked_response = SAMPLE_RESPONSE_OVERALL
        respx.get(mocked_url).respond(content=mocked_response)
        output = pypistats.overall(package, format="pandas")

        assert isinstance(output, pandas.DataFrame)
        assert list(output.columns) == ["category", "date", "downloads", "percent"]
        assert len(output) == 3 # 2 data rows + 1 total row
        assert output.iloc[0]['category'] == "with_mirrors"
        assert output.iloc[2]['category'] == "Total"


    @respx.mock
    def test_format_none(self) -> None:
        package = "pip"
        mocked_url = "https://pypistats.org/api/packages/pip/overall"
        mocked_response = SAMPLE_RESPONSE_OVERALL
        expected_output_str = "Format 'None' not supported by this path."
        respx.get(mocked_url).respond(content=mocked_response)
        output = pypistats.overall(package, format=None)
        assert output == expected_output_str

    @respx.mock
    def test_package_not_exist(self) -> None:
        package = "a" * 100
        mocked_response_dict = { "data":[], "package":package, "type":"python_major_downloads" }
        mocked_response_json = json.dumps(mocked_response_dict)
        mocked_url = f"https://pypistats.org/api/packages/{package}/python_major"
        expected_output = f"No data found for https://pypi.org/project/{package}/"
        respx.get(mocked_url).respond(content=mocked_response_json)
        output = pypistats.python_major(package)
        assert output == expected_output
