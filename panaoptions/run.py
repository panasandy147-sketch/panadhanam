#!/usr/bin/env python3
"""panaoptions — intraday US options paper trading on a small account.

    python run.py                     start the desk + dashboard (paper only)
    python run.py --no-web            the desk alone, terminal output only
    python run.py --once              run one cycle and exit
    python run.py --status            print the current state and exit
    python run.py --screen            run the pre-market screen and exit
    python run.py --explain-contracts what your budget actually buys, live
    python run.py --report [DAYS]     the paper-trading record so far
    python run.py --train [SYMBOLS]   train the optional ML filter
    python run.py --check             verify the data feed is reachable

Nothing here can place a real order. There is no broker adapter in this
package, by design.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _interpreter_with_dependencies() -> str:
    """A Python on this machine that probably has the packages installed.

    panaoptions sits beside panadhanam in the same repository, and that
    project's virtual environment already carries every package this one
    needs. Naming the exact interpreter beats telling somebody their
    environment is wrong and leaving them to work out which of the three
    Pythons on a Windows box was meant.
    """
    windows = sys.platform.startswith("win")
    folder, exe = ("Scripts", "python.exe") if windows else ("bin", "python")
    sep = "\\" if windows else "/"
    for base, label in ((HERE, "."), (HERE.parent, "..")):
        if (base / ".venv" / folder / exe).exists():
            return f"{label}{sep}.venv{sep}{folder}{sep}{exe}"
    return ""


def _require_dependencies() -> None:
    """Turn a bare ImportError into the command that fixes it."""
    try:
        import httpx  # noqa: F401
        import pandas  # noqa: F401
        import pydantic  # noqa: F401
        import yaml  # noqa: F401
        return
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "a required package"

    hint = _interpreter_with_dependencies()
    running = Path(sys.executable).name
    args = " ".join(sys.argv[1:])
    print(f"\n  '{missing}' is not installed for the Python you just used "
          f"({running}).\n")
    if hint:
        print("  The virtual environment beside this project already has every\n"
              "  package panaoptions needs:\n"
              f"\n      {hint} run.py {args}\n")
        print("  On Windows a bare `python` finds the Microsoft Store build,\n"
              "  which carries none of this project's packages.\n")
    else:
        print(f"  Install them with:\n\n      {running} -m pip install -r "
              f"requirements.txt\n")
    raise SystemExit(1)


_require_dependencies()

from panaoptions import clock  # noqa: E402
from panaoptions.config import Config, get_config  # noqa: E402
from panaoptions.logging import get_logger  # noqa: E402

log = get_logger("run")

BANNER = r"""
  ____                       ____        _   _
 |  _ \ __ _ _ __   __ _    / __ \ _ __ | |_(_) ___  _ __  ___
 | |_) / _` | '_ \ / _` |  | |  | | '_ \| __| |/ _ \| '_ \/ __|
 |  __/ (_| | | | | (_| |  | |__| | |_) | |_| | (_) | | | \__ \
 |_|   \__,_|_| |_|\__,_|   \____/| .__/ \__|_|\___/|_| |_|___/
                                  |_|
  Intraday US options — PAPER ONLY, no broker connection exists.
"""


async def _desk():
    from panaoptions.app import OptionsDesk
    return OptionsDesk()


async def _check() -> bool:
    from panaoptions.data.feed import YahooFeed
    cfg = get_config()
    print("\n=== DATA FEED ===")
    # The context manager connects on entry; calling connect() again here
    # would open a second client and print every failure twice.
    async with YahooFeed() as feed:
        ok = feed.connected
        if not ok:
            print("  Yahoo Finance UNREACHABLE — check your connection, a VPN, "
                  "or a corporate proxy.")
            return False
        print("  Yahoo Finance connected.")
        for symbol in cfg.symbols[:3]:
            quote = await feed.quote(symbol)
            if quote and quote.get("last_price"):
                print(f"  {symbol:5s} last {quote['last_price']:,.2f} "
                      f"prev close {quote.get('previous_close') or 0:,.2f}")
            else:
                print(f"  {symbol:5s} no quote")
        # Charts and option chains are different Yahoo hosts and fail
        # independently. This app BUYS OPTIONS, so a green chart feed with a
        # dead chain endpoint is not a working desk — it screens, charts and
        # fires setups all day and can never place one paper trade.
        print("\n=== OPTION CHAINS ===")
        if feed.options_available is False:
            print(f"  NOT AVAILABLE — {feed.options_error or 'empty response'}")
            print("  Charts work, so the screen and the strategies will run "
                  "normally and every setup will report 'no contract'.")
            print("  Nothing can be bought until this endpoint answers.")
            return False

        symbol = cfg.symbols[0]
        expiries = await feed.expiries(symbol)
        print(f"  {symbol} expiries listed: {len(expiries)}")
        if not expiries:
            print(f"  {feed.options_error or 'no expiry list returned'}")
            return False

        min_dte = int(cfg.get("contracts.min_dte", 7))
        max_dte = int(cfg.get("contracts.max_dte", 14))
        quote = await feed.quote(symbol)
        spot = (quote or {}).get("last_price") or 0.0
        chain = await feed.chain_for_window(symbol, spot, min_dte, max_dte)
        print(f"  {symbol} contracts at {min_dte}-{max_dte} DTE: {len(chain)}")
        if not chain:
            print(f"  {feed.options_error or 'the window matched no expiry'}")
            return False
        with_greeks = sum(1 for c in chain if c.delta)
        print(f"  of those, {with_greeks} carry a usable delta")
    return True


async def _explain_contracts() -> None:
    """What the configured budget actually buys, on live chains.

    This exists because the shipped delta band and the shipped price cap
    cannot both be satisfied on this universe, and an empty signal list is
    indistinguishable from a quiet market until somebody prints the numbers.
    """
    from panaoptions.data.feed import YahooFeed
    from panaoptions.data.greeks import atm_premium_estimate
    from panaoptions.engine.contracts import affordable_delta
    from panaoptions.models import Direction, OptionRight

    cfg = get_config()
    multiplier = cfg.multiplier
    min_price = float(cfg.get("contracts.min_contract_price", 0.60)) * multiplier
    max_price = float(cfg.get("contracts.max_contract_price", 1.00)) * multiplier
    min_delta = float(cfg.get("contracts.min_delta", 0.45))
    max_delta = float(cfg.get("contracts.max_delta", 0.60))
    min_dte = int(cfg.get("contracts.min_dte", 7))
    max_dte = int(cfg.get("contracts.max_dte", 14))

    print("\n=== WHAT YOUR BUDGET ACTUALLY BUYS ===")
    print(f"  Configured: delta {min_delta}-{max_delta}, "
          f"${min_price:.0f}-${max_price:.0f} per contract, {min_dte}-{max_dte} DTE\n")
    print(f"  {'Symbol':7s} {'Spot':>9s} {'ATM cost':>10s} "
          f"{'In budget':>10s} {'Delta you can afford':>22s}")
    print("  " + "-" * 62)

    impossible = []
    async with YahooFeed() as feed:
        if not await feed.connect():
            print("  No data feed — cannot check live prices.")
            return
        for symbol in cfg.symbols:
            quote = await feed.quote(symbol)
            spot = float((quote or {}).get("last_price") or 0)
            if not spot:
                print(f"  {symbol:7s} {'no quote':>9s}")
                continue

            chain = await feed.chain_for_window(symbol, spot, min_dte, max_dte)
            atm = [c for c in chain
                   if c.right is OptionRight.CALL
                   and min_delta <= abs(c.delta) <= max_delta and c.mid > 0]
            if atm:
                cheapest_atm = min(atm, key=lambda c: c.mid).cost(multiplier)
            else:
                iv = next((c.implied_volatility for c in chain
                           if c.implied_volatility), 0.25)
                cheapest_atm = atm_premium_estimate(spot, iv, 10) * multiplier

            band = affordable_delta(chain, Direction.LONG, cfg)
            fits = min_price <= cheapest_atm <= max_price
            if not fits:
                impossible.append(symbol)
            band_text = f"{band[0]:.2f}-{band[1]:.2f}" if band else "nothing in range"
            print(f"  {symbol:7s} {spot:9,.2f} {cheapest_atm:10,.0f} "
                  f"{('yes' if fits else 'NO'):>10s} {band_text:>22s}")

    if impossible:
        print(f"\n  {len(impossible)} of {len(cfg.symbols)} symbols cannot produce a "
              f"contract that is both {min_delta}-{max_delta} delta and under "
              f"${max_price:.0f}.")
        print("  These two rules describe an empty set, so the desk will keep\n"
              "  finding setups and taking none of them. That is the arithmetic\n"
              "  being honest, not a fault in the scanner.\n")
        print("  One contract is 100 shares, and 0.45-0.60 delta means at the\n"
              "  money. At-the-money costs what it costs.\n")
        print("  Pick one:")
        print("    1. Raise contracts.max_contract_price to what ATM actually")
        print("       costs above, and paper-trade the strategy as designed.")
        print("       Recommended: you are learning the rules, and the account")
        print("       size is the constraint, not the rules.")
        print("    2. Trade cheaper underlyings where ATM fits the budget.")
        print("    3. Lower contracts.min_delta to roughly 0.15-0.20 and accept")
        print("       out-of-the-money contracts. Theta and the spread will take")
        print("       most of the edge. Not recommended.\n")


async def _check_llm() -> int:
    """Is the journal's coach the model, or the rules fallback?

    `journal.use_llm: true` is safe to leave on — a dead Ollama falls back
    silently. That is exactly why it needs checking: "configured" and
    "working" look identical from the dashboard otherwise.
    """
    from panaoptions.ml.llm import probe

    cfg = get_config()
    on = bool(cfg.get("journal.use_llm", False))

    print("\n=== JOURNAL COACH ===")
    if not on:
        print("  journal.use_llm is false — cards are written by the rules.\n"
              "  That works; it is just not the model's reading.\n")
        return 0

    result = await probe(cfg)
    print(f"  Host   {result['host']}")
    print(f"  Model  {result.get('model', '—')}")
    if result["ok"]:
        print("\n  Working. Cards and the weekly review are written by the model.")
        print("  It writes the prose only — verdicts, scores, stops and sizes")
        print("  stay deterministic.\n")
        return 0

    print(f"\n  NOT working: {result['error']}\n")
    installed = result.get("installed")
    if installed:
        print(f"  Models you do have: {', '.join(installed)}")
        print(f"  Either pull the configured one, or point at one of these:\n"
              f"\n      {_interpreter_with_dependencies() or 'python'} run.py "
              f"--set OLLAMA_MODEL={installed[0]}\n")
    else:
        print("  Install Ollama from https://ollama.com/download, then:\n"
              f"\n      ollama serve\n      ollama pull {result.get('model')}\n")
    print("  Meanwhile every card falls back to the rules-written version, so\n"
          "  nothing is lost — the grading and the numbers are unaffected.\n")
    return 1


def _set_env(assignments: list[str]) -> int:
    """Write settings into .env, which survives a restart.

    An environment variable exported in a shell lasts until that shell closes.
    Setting capital that way works once and then silently reverts, taking the
    desk back to refusing every trade with no visible change in config.
    """
    from panaoptions.config import ENV_PATH, ROOT
    from panaoptions.envfile import mask, parse_assignment, write

    try:
        updates = dict(parse_assignment(a) for a in assignments)
    except ValueError as exc:
        print(f"\n  {exc}\n\n  Expected: --set KEY=VALUE\n")
        return 2

    existed = ENV_PATH.exists()
    if not existed and (ROOT / ".env.example").exists():
        ENV_PATH.write_text((ROOT / ".env.example").read_text(encoding="utf-8"),
                            encoding="utf-8")

    outcome = write(ENV_PATH, updates)
    print(f"\n=== {ENV_PATH} ===")
    if not existed:
        print("  created from .env.example")
    for key, value in updates.items():
        print(f"  {key:<24} {mask(key, value):<20} ({outcome[key]})")

    print("\n  Re-checking whether every rule can hold...\n")
    get_config().reload()
    return _check_config()


def _check_config() -> int:
    """Can every rule hold at once? Run this after changing any of them."""
    from panaoptions import preflight

    cfg = get_config()
    findings = preflight.report(cfg, log_it=False)
    multiplier = cfg.multiplier
    budget = cfg.capital * float(cfg.get("risk.max_capital_deployed_pct", 20)) / 100

    print("\n=== CONFIGURATION CHECK ===")
    print(f"  Profile           {cfg.profile_label}")
    print(f"  Bars              {cfg.get('technical.timeframe')} "
          f"(patterns on "
          f"{cfg.get('strategies.candlestick_at_level.timeframe', '15m')})")
    print(f"  Expiries          {cfg.get('contracts.min_dte')}-"
          f"{cfg.get('contracts.max_dte')} DTE")
    print(f"  Entries           {cfg.get('session.entry_open')}-"
          f"{cfg.last_entry_hhmm}, square off "
          f"{cfg.get('session.force_exit_at')}")
    print(f"  Screen            gap >= |{cfg.get('premarket.min_gap_pct')}|%, "
          f"RVOL >= {cfg.get('premarket.min_rvol')}")
    print(f"  Capital           ${cfg.capital:,.2f}")
    print(f"  Deployed per trade ${budget:,.2f} "
          f"({cfg.get('risk.max_capital_deployed_pct')}%)")
    print(f"  At risk per trade  ${budget * float(cfg.get('risk.stop_loss_pct', 20)) / 100:,.2f} "
          f"(that budget behind a {cfg.get('risk.stop_loss_pct')}% stop)")
    print(f"  Contract price cap ${float(cfg.get('contracts.max_contract_price', 0)) * multiplier:,.0f}")
    print(f"  Delta band         {cfg.get('contracts.min_delta')}-{cfg.get('contracts.max_delta')}")
    print(f"  Universe           {', '.join(cfg.symbols)}\n")

    if not findings:
        print("  Every rule can hold at once. Nothing here will stop a trade.\n")
        return 0

    for finding in findings:
        print(finding.render())
        print()

    blockers = [f for f in findings if f.level == "blocker"]
    if blockers:
        print(f"  {len(blockers)} blocker(s). The desk will scan and take nothing\n"
              "  until these are resolved — which looks exactly like a quiet\n"
              "  market, so fix them before you judge the strategy.\n")
        commands = [f.command for f in blockers if f.command]
        if commands:
            print("  The short version:\n")
            for command in commands:
                print(f"      {command}")
            print()
        return 1
    print("  Warnings only — the desk will trade.\n")
    return 0


async def _screen() -> None:
    from panaoptions.data.feed import YahooFeed
    from panaoptions.data.premarket import screen

    cfg = get_config()
    now = clock.now(cfg.timezone)
    async with YahooFeed() as feed:
        if not await feed.connect():
            return
        reads = await screen(feed, cfg, now)

    print(f"\n=== PRE-MARKET SCREEN  {now:%Y-%m-%d %H:%M %Z} ===")
    print(f"  {'Symbol':7s} {'Last':>9s} {'Gap %':>8s} {'RVOL':>7s}  Verdict")
    print("  " + "-" * 62)
    for r in sorted(reads, key=lambda r: (not r.passed, -abs(r.gap_pct))):
        print(f"  {r.symbol:7s} {r.last_price:9,.2f} {r.gap_pct:+8.2f} "
              f"{r.rvol:7.2f}  {'PASS' if r.passed else 'skip'}")
        for reason in r.reasons:
            print(f"          {reason}")


def _report(days: int) -> None:
    from datetime import date, timedelta

    from panaoptions.ledger import store

    store.init()
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = store.trades(limit=1000, since=since)
    sessions = store.sessions(limit=days)

    print(f"\n=== PAPER TRADING RECORD — last {days} days ===")
    if not rows:
        print("  No trades recorded yet.\n")
        tally = store.rejection_tally(since)
        if tally:
            print("  Setups considered and passed over:")
            for reason, count in list(tally.items())[:8]:
                print(f"    {count:4d}x  {reason}")
            print("\n  If one reason dominates, that is the rule to look at.")
            print("  `python run.py --explain-contracts` checks the usual suspect.")
        return

    wins = [r for r in rows if (r["realised_pnl"] or 0) > 0]
    total = sum(r["realised_pnl"] or 0 for r in rows)
    print(f"  Trades: {len(rows)} | Wins: {len(wins)} "
          f"({len(wins) / len(rows) * 100:.0f}%) | P&L: ${total:+,.2f}")
    print(f"  Sessions traded: {len(sessions)}\n")
    print(f"  {'Date':11s} {'Symbol':7s} {'Contract':26s} {'Exit':16s} {'P&L':>9s}")
    print("  " + "-" * 74)
    for r in rows[:40]:
        print(f"  {r['session_date']:11s} {r['symbol']:7s} "
              f"{(r['contract'] or '')[:26]:26s} {(r['exit_reason'] or ''):16s} "
              f"{r['realised_pnl'] or 0:+9.2f}")
    print()


async def _train(symbols: list[str]) -> int:
    import pandas as pd

    from panaoptions.data.feed import YahooFeed
    from panaoptions.engine import indicators as ta
    from panaoptions.ml.train import MissingDependencies, dataset, save, walk_forward

    cfg = get_config()
    symbols = symbols or cfg.symbols

    try:
        frames = []
        async with YahooFeed() as feed:
            if not await feed.connect():
                return 1
            for symbol in symbols:
                bars = await feed.candles(symbol, "5m")
                daily = await feed.candles(symbol, "1d")
                if len(bars) < 500:
                    print(f"  {symbol}: only {len(bars)} bars, skipping")
                    continue
                frame = dataset(ta.to_frame(bars), ta.to_frame(daily), cfg)
                frame["symbol"] = symbol
                frames.append(frame)
                print(f"  {symbol}: {len(frame):,} labelled rows "
                      f"({frame['label'].mean() * 100:.1f}% positive)")

        if not frames:
            print("\n  No usable data. Yahoo serves only ~60 days of 5m bars,\n"
                  "  which may be too short for the configured walk-forward.\n")
            return 1

        combined = pd.concat(frames).sort_index()
        model, report = walk_forward(combined, cfg, symbol=",".join(symbols))
        paths = save(model, report)

        summary = report.to_dict()
        print(f"\n  Folds: {len(summary['folds'])} | mean AUC: {summary['mean_auc']}")
        print(f"  Precision at p>={report.threshold}: "
              f"{summary['mean_precision_at_threshold']}")
        print("\n  Top features:")
        for name, score in list(report.feature_importance.items())[:8]:
            print(f"    {name:24s} {score:.4f}")
        print(f"\n  Saved: {paths['model']}")
        print("  Set ml.enabled: true in config/settings.yaml to use it.\n")
        return 0
    except MissingDependencies as exc:
        print(f"\n  {exc}\n")
        return 1
    except ValueError as exc:
        print(f"\n  {exc}\n")
        return 1


def _list_profiles() -> int:
    """What profiles exist, and what each one changes about the desk."""
    from panaoptions.config import PROFILE_DIR, available_profiles

    names = available_profiles()
    print("=== PROFILES ===")
    print("  default            5m/15m bars, 7-45 DTE contracts. The shipped desk.")
    if not names:
        print(f"  (no profiles installed in {PROFILE_DIR})")
        return 0
    for name in names:
        cfg = Config(profile=name)
        print(f"  {name:18} {cfg.get('technical.timeframe')} bars, "
              f"{cfg.get('contracts.min_dte')}-{cfg.get('contracts.max_dte')} DTE, "
              f"entries {cfg.get('session.entry_open')}-{cfg.last_entry_hhmm}, "
              f"universe {', '.join(cfg.symbols)}")
    print()
    print("  Run one with:  python run.py --profile <name>")
    print("  A profile is a DIFFERENT DESK, not a tuning — check what it")
    print("  changes with:  python run.py --profile <name> --check-config")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="panaoptions — paper options desk")
    parser.add_argument("--once", action="store_true", help="one cycle, then exit")
    parser.add_argument("--status", action="store_true", help="print state and exit")
    parser.add_argument("--screen", action="store_true", help="pre-market screen only")
    parser.add_argument("--explain-contracts", action="store_true",
                        help="what your budget actually buys, on live chains")
    parser.add_argument("--report", nargs="?", const=30, type=int, metavar="DAYS",
                        help="the paper-trading record (default 30 days)")
    parser.add_argument("--train", nargs="*", metavar="SYMBOL",
                        help="train the optional ML filter")
    parser.add_argument("--check", action="store_true", help="verify the data feed")
    parser.add_argument("--check-llm", action="store_true",
                        help="is Ollama actually writing the journal cards?")
    parser.add_argument("--check-config", action="store_true",
                        help="can all the rules hold at once?")
    # action="extend" matters: with a plain nargs="+" argparse keeps only the
    # LAST --set on the line and silently drops the rest.
    parser.add_argument("--set", nargs="+", action="extend", metavar="KEY=VALUE",
                        dest="set_env",
                        help="write settings into .env; repeatable")
    parser.add_argument("--profile", metavar="NAME",
                        help="config profile to layer over settings.yaml "
                             "(e.g. scalp — a 1-minute, 0-DTE desk)")
    parser.add_argument("--profiles", action="store_true",
                        help="list the available profiles and exit")
    parser.add_argument("--interval", type=int, default=60,
                        help="seconds between cycles (default 60)")
    parser.add_argument("--no-web", action="store_true",
                        help="run the desk without the dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    # 8100, not 8000: panadhanam's dashboard already owns 8000, and two desks
    # fighting over a port is a confusing way to find that out.
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    if args.profiles:
        raise SystemExit(_list_profiles())

    # Selected before anything reads the configuration, so every command below
    # — including --check-config — reports the desk that will actually run.
    if args.profile:
        try:
            get_config(args.profile)
        except FileNotFoundError as exc:
            print(exc)
            raise SystemExit(2) from None

    if args.set_env:
        raise SystemExit(_set_env(args.set_env))
    if args.check_config:
        raise SystemExit(_check_config())
    if args.check_llm:
        raise SystemExit(asyncio.run(_check_llm()))
    if args.check:
        raise SystemExit(0 if asyncio.run(_check()) else 1)
    if args.explain_contracts:
        asyncio.run(_explain_contracts())
        return
    if args.screen:
        asyncio.run(_screen())
        return
    if args.report is not None:
        _report(args.report)
        return
    if args.train is not None:
        raise SystemExit(asyncio.run(_train(args.train)))

    if args.status:
        desk = asyncio.run(_desk())
        print(json.dumps(desk.status(), indent=2, default=str))
        return

    print(BANNER)
    desk = asyncio.run(_desk())
    if args.once:
        from panaoptions.ledger import store
        store.init()
        asyncio.run(_run_once(desk))
        return

    if args.no_web:
        try:
            asyncio.run(desk.start(cycle_seconds=args.interval))
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    _serve(desk, args.host, args.port, args.interval)


def _serve(desk, host: str, port: int, interval: int) -> None:
    """Run the desk with the dashboard in front of it."""
    try:
        import uvicorn
    except ImportError:
        print("\n  The dashboard needs fastapi and uvicorn:\n"
              f"\n      {_interpreter_with_dependencies() or 'python'} "
              "-m pip install fastapi uvicorn\n"
              "\n  Or run the desk without it:  run.py --no-web\n")
        raise SystemExit(1) from None

    from panaoptions.web.server import create_app

    app = create_app(desk, cycle_seconds=interval)
    app.state.port = port
    print(f"  Dashboard  →  http://{host}:{port}")
    print("  Leave this window open; closing it stops the desk.\n")
    try:
        uvicorn.run(app, host=host, port=port, log_config=None)
    except KeyboardInterrupt:
        print("\nStopped.")


async def _run_once(desk) -> None:
    if not await desk.feed.connect():
        return
    try:
        result = await desk.cycle()
        print(json.dumps(result, indent=2, default=str))
    finally:
        await desk.feed.close()


if __name__ == "__main__":
    main()
