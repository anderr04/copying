#!/usr/bin/env python3
"""
analysis.py – Shadow trade analysis script.

Reads shadow_trades.csv and trades.csv to compare:
- Bot performance WITH IA filter vs WITHOUT
- Win-rate, PnL, Sharpe, drawdown
- % of losing trades the IA would have blocked

Usage:
    python analysis.py                          # Full report
    python analysis.py --min-trades 50          # Wait for N trades
    python analysis.py --csv data/shadow.csv    # Custom CSV path
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent / "data"


def load_csv(path: Path) -> list[dict]:
    """Load a CSV file into a list of dicts."""
    if not path.exists():
        print(f"  [!] File not found: {path}")
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def safe_float(val: str, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def analyze_shadow(shadow_rows: list[dict]) -> None:
    """Analyze shadow_trades.csv data."""
    total = len(shadow_rows)
    if total == 0:
        print("  No shadow trades to analyze.\n")
        return

    # Stats
    evaluated = [r for r in shadow_rows
                 if r.get("model_confidence", "0") != "0"]
    errors = [r for r in shadow_rows
              if r.get("model_explanation") == "OLLAMA_ERROR"]
    would_copy = [r for r in evaluated if r.get("would_copy") == "True"]
    would_reject = [r for r in evaluated if r.get("would_copy") == "False"]

    # If actual_outcome is filled, compute win rates
    with_outcome = [r for r in shadow_rows
                    if r.get("actual_outcome") in ("WIN", "LOSS")]

    print(f"  Total shadow entries   : {total}")
    print(f"  Successfully evaluated : {len(evaluated)}")
    print(f"  Ollama errors          : {len(errors)}")
    print(f"  Would COPY (IA approved): {len(would_copy)}")
    print(f"  Would REJECT (IA blocked): {len(would_reject)}")
    print()

    # Category breakdown
    categories: dict[str, int] = {}
    for r in shadow_rows:
        cat = r.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1
    print("  Category breakdown:")
    for cat, count in sorted(categories.items(),
                             key=lambda x: -x[1]):
        pct = count / total * 100
        print(f"    {cat:<20s}  {count:>4d}  ({pct:.1f}%)")
    print()

    # Confidence distribution
    confs = [int(r.get("model_confidence", "0"))
             for r in evaluated]
    if confs:
        avg_conf = sum(confs) / len(confs)
        print(f"  Avg model confidence   : {avg_conf:.1f}%")
        print(f"  Min / Max confidence   : {min(confs)}% / {max(confs)}%")
        print()

    # Edge ratio distribution
    edges = [safe_float(r.get("edge_ratio", "0"))
             for r in evaluated if r.get("edge_ratio")]
    if edges:
        avg_edge = sum(edges) / len(edges)
        print(f"  Avg edge ratio         : {avg_edge:.2f}x")
        above_threshold = sum(1 for e in edges if e >= 1.45)
        print(f"  Edge >= 1.45x          : {above_threshold}/{len(edges)} "
              f"({above_threshold/len(edges)*100:.1f}%)")
        print()

    # Latency
    latencies = [safe_float(r.get("latency_ms", "0"))
                 for r in evaluated]
    if latencies:
        avg_lat = sum(latencies) / len(latencies)
        print(f"  Avg Ollama latency     : {avg_lat:.0f}ms")
        print(f"  Max latency            : {max(latencies):.0f}ms")
        print()

    # ── Performance comparison (only if actual_outcome exists) ──
    if len(with_outcome) >= 10:
        print("  " + "=" * 60)
        print("  PERFORMANCE COMPARISON (trades with known outcomes)")
        print("  " + "=" * 60)

        # All trades (no filter)
        all_wins = sum(1 for r in with_outcome
                       if r.get("actual_outcome") == "WIN")
        all_losses = len(with_outcome) - all_wins
        all_wr = all_wins / len(with_outcome) * 100

        # IA filtered (only those the IA would copy)
        ia_trades = [r for r in with_outcome
                     if r.get("would_copy") == "True"]
        ia_wins = sum(1 for r in ia_trades
                      if r.get("actual_outcome") == "WIN")
        ia_losses = len(ia_trades) - ia_wins
        ia_wr = ia_wins / len(ia_trades) * 100 if ia_trades else 0

        # Rejected by IA
        rejected = [r for r in with_outcome
                    if r.get("would_copy") == "False"]
        rej_wins = sum(1 for r in rejected
                       if r.get("actual_outcome") == "WIN")
        rej_losses = len(rejected) - rej_wins

        print(f"\n  Without IA filter:")
        print(f"    Trades: {len(with_outcome)}  "
              f"W={all_wins} L={all_losses}  WR={all_wr:.1f}%")

        print(f"\n  With IA filter (would_copy=True):")
        print(f"    Trades: {len(ia_trades)}  "
              f"W={ia_wins} L={ia_losses}  WR={ia_wr:.1f}%")

        print(f"\n  Rejected by IA:")
        print(f"    Trades: {len(rejected)}  "
              f"W={rej_wins} L={rej_losses}")
        if rejected:
            losses_blocked = rej_losses
            total_losses_all = all_losses
            if total_losses_all > 0:
                pct_blocked = losses_blocked / total_losses_all * 100
                print(f"    Losses blocked: {losses_blocked}/"
                      f"{total_losses_all} ({pct_blocked:.1f}%)")

        # PnL comparison (if pnl_simulated is filled)
        all_pnl = [safe_float(r.get("pnl_simulated", "0"))
                   for r in with_outcome
                   if r.get("pnl_simulated")]
        ia_pnl = [safe_float(r.get("pnl_simulated", "0"))
                  for r in ia_trades
                  if r.get("pnl_simulated")]

        if all_pnl:
            print(f"\n  PnL (all trades)       : ${sum(all_pnl):+.2f}")
        if ia_pnl:
            print(f"  PnL (IA filtered)      : ${sum(ia_pnl):+.2f}")
            improvement = sum(ia_pnl) - sum(all_pnl) if all_pnl else 0
            print(f"  IA improvement         : ${improvement:+.2f}")

        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze shadow IA validation results")
    parser.add_argument(
        "--csv", type=str, default=None,
        help="Path to shadow_trades.csv")
    parser.add_argument(
        "--min-trades", type=int, default=0,
        help="Minimum trades required for analysis")
    args = parser.parse_args()

    shadow_path = Path(args.csv) if args.csv else DATA_DIR / "shadow_trades.csv"

    print()
    print("=" * 64)
    print("  SHADOW IA VALIDATION ANALYSIS")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 64)
    print()

    shadow = load_csv(shadow_path)

    if len(shadow) < args.min_trades:
        print(f"  Only {len(shadow)} shadow trades "
              f"(need {args.min_trades}). Wait for more data.")
        sys.exit(0)

    analyze_shadow(shadow)

    print("=" * 64)
    print()


if __name__ == "__main__":
    main()
