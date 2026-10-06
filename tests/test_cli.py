"""The command line, config.toml and how `years` becomes a period. Offline."""
from datetime import date
import importlib
from pathlib import Path

import pytest

from stock_crawler import cli
from stock_crawler.dates import years_start
from stock_crawler.fundamentals import crawler as fundamentals
from stock_crawler.market_index import crawler as market_index

CONFIG = Path(__file__).resolve().parent.parent / "config" / "config.toml"


def test_config_section_becomes_options():
    config = {"years": 5, "fundamentals": {"base_url": "https://x", "update": True, "refresh": False,
                                           "symbols": "", "sectors": ["GENERAL", "BANKS"]}}
    assert cli.config_argv("fundamentals", config) == [
        "--base-url", "https://x", "--update", "--sectors", "GENERAL", "BANKS", "--years", "5"]


def test_home_directory_in_config_paths_is_expanded():
    argv = cli.config_argv("companies", {"companies": {"ca_bundle": "~/roots.pem"}})
    assert argv == ["--ca-bundle", str(Path.home() / "roots.pem")]


def test_years_only_for_commands_with_history():
    config = {"years": 10, "companies": {"market_id": 1}, "sovereign": {"years": 20}}
    assert cli.config_argv("companies", config) == ["--market-id", "1"]
    assert cli.config_argv("sovereign", config) == ["--years", "20"]  # its own setting wins
    assert cli.config_argv("market_index", config) == ["--years", "10"]


@pytest.mark.parametrize("command", cli.COMMANDS)
def test_shipped_config_is_accepted_by_every_command(command):
    """Catches a typo in config.toml, or an option renamed in the code but not in the file."""
    config = cli.load_config(CONFIG)
    module = importlib.import_module(cli.COMMANDS[command][0])
    module.parse_args(cli.config_argv(command, config))


def test_command_line_wins_over_config():
    config = cli.load_config(CONFIG)
    args = market_index.parse_args(cli.config_argv("market_index", config) + ["--years", "1", "--end", "2026-09-25"])
    assert args.years == 1 and args.start == date(2025, 1, 1)


def test_unknown_config_section_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('years = 5\n[fundamental]\nmin_delay = 30\n', encoding="utf-8")
    with pytest.raises(ValueError, match="fundamental"):
        cli.load_config(path)


def test_run_all_rejects_unknown_command():
    assert cli.run_all({"run_all": ["companies", "prices"]}, []) == 2


def test_years_start_on_1_january():
    assert years_start(date(2026, 10, 6), 10) == date(2016, 1, 1)
    assert years_start(date(2024, 2, 29), 1) == date(2023, 1, 1)


def test_market_index_start_from_years():
    args = market_index.parse_args(["--years", "2", "--end", "2026-09-25"])
    assert args.start == date(2024, 1, 1)
    args = market_index.parse_args(["--years", "2", "--start", "2026-01-01", "--end", "2026-09-25"])
    assert args.start == date(2026, 1, 1)  # --start overrides --years


@pytest.mark.parametrize("argv, expected", [
    (["--years", "5"], list(range(2021, 2027))),                     # 1 January five years ago
    (["--years", "1"], [2025, 2026]),
    (["--years", "20"], list(range(fundamentals.FIRST_YEAR, 2027))),  # KAP has nothing earlier
    (["--start", "2019-06-30", "--end", "2020-01-01"], [2019, 2020]),
    (["--years", "10", "--update"], [2025, 2026]),
])
def test_fundamentals_fiscal_years(argv, expected):
    assert fundamentals.fiscal_years(fundamentals.parse_args(argv), date(2026, 9, 26)) == expected


def test_fundamentals_fetches_newest_years_first():
    assert fundamentals.newest_first(list(range(2016, 2027))) == [
        [2025, 2026], [2023, 2024], [2021, 2022], [2019, 2020], [2017, 2018], [2016]]
    assert fundamentals.newest_first([2025, 2026]) == [[2025, 2026]]  # --update asks the same pair
