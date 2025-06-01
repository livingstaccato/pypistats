"""
Unit tests for cli
"""

from __future__ import annotations

# import argparse # Removed
import click # Added
from click.testing import CliRunner # Added
import pytest
from freezegun import freeze_time

from pypistats import cli
# Import the specific items to be tested if they are not top-level in cli module
# For example, if YYYY_MM_DD_OPTIONAL is in pypistats.cli:
# from pypistats.cli import YYYY_MM_DD_OPTIONAL, YYYY_MM, validate_python_major, validate_python_minor


@pytest.mark.parametrize(
    "test_input, expected",
    [
        ("2018-07", ("2018-07-01", "2018-07-31")),
        ("2018-12", ("2018-12-01", "2018-12-31")),
    ],
)
def test__month(test_input: str, expected: str) -> None:
    # Act
    first, last = cli._month(test_input)

    # Assert
    assert expected == (first, last)


@pytest.mark.parametrize(
    "test_input, expected",
    [
        ("2018-01-25", ("2017-12-01", "2017-12-31")),
        ("2018-09-25", ("2018-08-01", "2018-08-31")),
        ("2018-12-25", ("2018-11-01", "2018-11-30")),
    ],
)
def test__last_month(test_input: str, expected: str) -> None:
    # Act
    with freeze_time(test_input):
        first, last = cli._last_month()

    # Assert
    assert expected == (first, last)


@pytest.mark.parametrize(
    "test_input, expected",
    [
        ("2019-03-10", "2019-03-01"),
        ("2019-05-08", "2019-05-01"),
        ("2019-12-25", "2019-12-01"),
    ],
)
def test__this_month(test_input: str, expected: str) -> None:
    # Act
    with freeze_time(test_input):
        first = cli._this_month()

    # Assert
    assert expected == first


@freeze_time("2019-05-08")
@pytest.mark.parametrize(
    "name, date_format, expected",
    [
        ("jan", "%b", "2019-01"),
        ("Jan", "%b", "2019-01"),
        ("january", "%B", "2019-01"),
        ("January", "%B", "2019-01"),
        ("feb", "%b", "2019-02"),
        ("february", "%B", "2019-02"),
        ("may", "%b", "2019-05"),
        ("dec", "%b", "2018-12"),
        ("december", "%B", "2018-12"),
    ],
)
def test__month_name_to_yyyy_mm(name: str, date_format: str, expected: str) -> None:
    # Act
    output = cli._month_name_to_yyyy_mm(name, date_format)

    # Assert
    assert expected == output

# Test for failure case of _month_name_to_yyyy_mm
@freeze_time("2019-05-08")
def test__month_name_to_yyyy_mm_invalid():
    with pytest.raises(ValueError) as excinfo:
        cli._month_name_to_yyyy_mm("InvalidMonth", "%b")
    assert "Could not parse month 'InvalidMonth' for current or previous year." in str(excinfo.value)

# Obsolete tests for _valid_yyyy_mm_dd, _valid_yyyy_mm, _valid_yyyy_mm_optional_dd are removed.
# Obsolete tests for _define_format are removed.
# Obsolete tests for _python_major_version and _python_minor_version helpers are removed.
# __Args helper class is removed.

# --- New tests for click custom types and callbacks ---
runner = CliRunner()

# Helper command for testing date types
@click.command()
@click.option("--date1", type=cli.YYYY_MM_DD_OPTIONAL) # Assuming YYYY_MM_DD_OPTIONAL is accessible via cli module
@click.option("--date2", type=cli.YYYY_MM) # Assuming YYYY_MM is accessible via cli module
def check_date_types_command(date1, date2):
    if date1: click.echo(f"date1:{date1}")
    if date2: click.echo(f"date2:{date2}")

@pytest.mark.parametrize("param_name, value, expected_output_part", [
    ("--date1", "2023-10-25", "date1:2023-10-25"),
    ("--date1", "2023-10", "date1:2023-10"),
    pytest.param("--date1", "oct", "date1:2023-10", marks=freeze_time("2023-11-01")),
    pytest.param("--date1", "dec", "date1:2022-12", marks=freeze_time("2023-01-15")),
    ("--date2", "2023-09", "date2:2023-09"),
    pytest.param("--date2", "sep", "date2:2023-09", marks=freeze_time("2023-10-01")),
])
def test_click_date_param_types_valid(param_name, value, expected_output_part):
    result = runner.invoke(check_date_types_command, [param_name, value])
    assert result.exit_code == 0, f"Output: {result.output}, Exception: {result.exception}"
    assert expected_output_part in result.output

@pytest.mark.parametrize("param_name, value, expected_error_part", [
    ("--date1", "2023-10-32", "not a valid yyyy-mm-dd"),
    ("--date1", "2023-13", "not a valid yyyy-mm"),
    ("--date1", "invalid-month-name", "not a valid yyyy-mm-dd"),
    ("--date2", "2023-10-25", "not a valid yyyy-mm format"),
    ("--date2", "invalid-month", "not a valid yyyy-mm format"),
])
def test_click_date_param_types_invalid(param_name, value, expected_error_part):
    result = runner.invoke(check_date_types_command, [param_name, value])
    assert result.exit_code != 0, "Command should have failed"
    assert expected_error_part in result.output

# Helper command for testing version callbacks
@click.command()
@click.option("--major", callback=cli.validate_python_major) # Assuming validate_python_major is accessible
@click.option("--minor", callback=cli.validate_python_minor) # Assuming validate_python_minor is accessible
def check_py_versions_command(major, minor):
    if major: click.echo(f"major:{major}")
    if minor: click.echo(f"minor:{minor}")

@pytest.mark.parametrize("param_name, value, expected_output_part", [
    ("--major", "3", "major:3"),
    ("--minor", "3.10", "minor:3.10"),
])
def test_click_py_version_callbacks_valid(param_name, value, expected_output_part):
    result = runner.invoke(check_py_versions_command, [param_name, value])
    assert result.exit_code == 0, f"Output: {result.output}, Exception: {result.exception}"
    assert expected_output_part in result.output

@pytest.mark.parametrize("param_name, value, expected_error_part", [
    ("--major", "3.1", "must be an integer"),
    ("--major", "abc", "must be an integer"),
    ("--minor", "3", "must be in X.Y format"),
    ("--minor", "abc", "must be in X.Y format"),
])
def test_click_py_version_callbacks_invalid(param_name, value, expected_error_part):
    result = runner.invoke(check_py_versions_command, [param_name, value])
    assert result.exit_code != 0, "Command should have failed"
    assert expected_error_part in result.output


@freeze_time("2019-05-08")
@pytest.mark.parametrize(
    "test_input, expected",
    [
        ("jan", "2019-01"),
        ("Jan", "2019-01"),
        ("january", "2019-01"),
        ("January", "2019-01"),
        ("feb", "2019-02"),
        ("february", "2019-02"),
        ("may", "2019-05"),
        ("dec", "2018-12"),
        ("december", "2018-12"),
    ],
)
def test__valid_yyyy_mm_valid_name(test_input: str, expected: str) -> None:
    assert expected == cli._valid_yyyy_mm(test_input)


