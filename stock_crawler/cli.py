"""Command line: python -m stock_crawler [--config FILE] COMMAND [options].

Settings come from config/config.toml. Every key in a command's [section] is one of that
command's options (`market_id = 1` is `--market-id 1`), and options typed on the command
line win over the file. The top-level `years` is passed to every command that fetches
history, unless its section sets its own.
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
from pathlib import Path
import signal
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

# command -> (module, description, takes --years)
COMMANDS = {
    "companies": ("stock_crawler.companies.crawler", "listed companies (KAP + Borsa İstanbul)", False),
    "market_data": ("stock_crawler.market_data.crawler",
                    "daily share prices with USD (Borsa İstanbul bulletin + TCMB)", True),
    "check_market_data": ("stock_crawler.market_data.check", "check the market_data CSV for errors", False),
    "fundamentals": ("stock_crawler.fundamentals.crawler", "quarterly financial statement items (KAP)", True),
    "fundamental_reports": ("stock_crawler.fundamental_reports.crawler",
                            "Ebitda, debt, cash, FCF, EPS, shares from KAP full reports", True),
    "market_index": ("stock_crawler.market_index.crawler", "BIST index daily closes (İş Yatırım)", True),
    "sovereign": ("stock_crawler.sovereign.crawler", "Türkiye macro data (CBRT EVDS + Treasury)", True),
}
TOP_LEVEL_KEYS = {"years", "run_all", "log_dir"}
DEFAULT_CONFIG = Path("config/config.toml")
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_DATE = "%Y-%m-%d %H:%M:%S"
log = logging.getLogger("stock_crawler")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    commands = "\n".join(f"  {name:<19}{description}" for name, (_, description, _) in COMMANDS.items())
    p = argparse.ArgumentParser(
        prog="python -m stock_crawler",
        description="Crawl Borsa İstanbul and Türkiye data into CSV files.",
        epilog=f"commands:\n{commands}\n  {'all':<19}run the commands listed in run_all in the config\n\n"
               "Options for one command: python -m stock_crawler COMMAND --help",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="settings file (default: %(default)s)")
    p.add_argument("command", choices=[*COMMANDS, "all"], metavar="COMMAND")
    p.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def load_config(path: Path) -> dict:
    with path.open("rb") as source:
        config = tomllib.load(source)
    unknown = sorted(config.keys() - COMMANDS.keys() - TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"unknown setting(s) {', '.join(unknown)}; sections must be one of {', '.join(COMMANDS)}")
    return config


def config_argv(command: str, config: dict) -> list[str]:
    """The command's config section as command-line options."""
    section = dict(config.get(command, {}))
    if COMMANDS[command][2] and "years" in config:
        section.setdefault("years", config["years"])
    argv: list[str] = []
    for key, value in section.items():
        option = "--" + key.replace("_", "-")
        if value is True:
            argv.append(option)
        elif value is False or value == "" or value == []:
            continue  # switched off / not set: the command's own default applies
        elif isinstance(value, list):
            argv += [option, *map(str, value)]
        else:
            argv += [option, os.path.expanduser(value) if isinstance(value, str) else str(value)]
    return argv


def run(command: str, config: dict, extra: list[str]) -> int:
    """Run one command with its config options plus `extra`; also log to <log_dir>/<command>.log."""
    argv = config_argv(command, config) + extra
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if "-v" in argv or "--verbose" in argv else logging.INFO)
    log_file = Path(config.get("log_dir", "logs")) / f"{command}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE))
    root.addHandler(file_handler)
    try:
        return importlib.import_module(COMMANDS[command][0]).main(argv)
    except Exception:  # a bug, not a network problem: keep the traceback in the log
        log.exception("%s stopped with an unexpected error", command)
        return 1
    finally:
        root.removeHandler(file_handler)
        file_handler.close()


def run_all(config: dict, extra: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m stock_crawler all",
                                description="Run the commands listed in run_all in the config, in order.")
    p.add_argument("--years", type=int, help="history for every command that uses it (overrides the config)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(extra)
    commands = config.get("run_all", [c for c in COMMANDS if c != "check_market_data"])
    if unknown := [c for c in commands if c not in COMMANDS]:
        log.error("run_all: unknown command(s) %s", ", ".join(unknown))
        return 2

    results: dict[str, tuple[int, float]] = {}
    for command in commands:
        options = ["--years", str(args.years)] if args.years and COMMANDS[command][2] else []
        if args.verbose and command != "check_market_data":
            options.append("--verbose")
        log.info("========== %s ==========", command)
        started = time.monotonic()
        try:
            code = run(command, config, options)
        except SystemExit as exc:  # the command rejected its options (see the message above)
            code = exc.code if isinstance(exc.code, int) else 2
        results[command] = code, (time.monotonic() - started) / 60
        if code == 130:
            log.warning("Interrupted; the remaining commands were not run")
            break

    log.info("========== summary ==========")
    for command, (code, minutes) in results.items():
        log.info("%-18s %-8s %6.1f min", command, "ok" if code == 0 else f"exit {code}", minutes)
    codes = [code for code, _ in results.values()]
    return 130 if 130 in codes else int(any(codes))


def _interrupt(*_) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:  # tomllib.TOMLDecodeError is a ValueError
        log.error("Cannot read %s: %s (run from the project folder, or pass --config)", args.config, exc)
        return 2
    signal.signal(signal.SIGTERM, _interrupt)  # `kill` stops a crawler like Ctrl-C; caches are kept
    if hasattr(signal, "SIGHUP"):  # so does closing its terminal (not on Windows)
        signal.signal(signal.SIGHUP, _interrupt)
    try:
        if args.command == "all":
            return run_all(config, args.args)
        return run(args.command, config, args.args)
    except KeyboardInterrupt:
        log.warning("Interrupted")
        return 130
