"""
logger.py - Trade logging to CSV and structured console output.

Every closed round-trip trade is appended to data/trades.csv so that
post-session analysis (e.g. with pandas) is straightforward.
"""

from __future__ import annotations

import csv
import logging
import logging.handlers
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
from src.paper_trader import Position

logger = logging.getLogger(__name__)

# CSV column order (v3 adds last 4 orderbook-analysis columns)
CSV_COLUMNS = [
    "timestamp_open",
    "timestamp_close",
    "market_id",
    "market_question",
    "side",
    "entry_price",
    "exit_price",
    "shares",
    "cost",
    "pnl",
    "exit_reason",
    "capital_after",
    # ── v3 orderbook metrics ──
    "ob_imbalance",
    "yes_liquidity",
    "no_liquidity",
    "entry_spread_pct",
    # ── v3 data-science indicators ──
    "time_elapsed_s",
    "spike_velocity",
    "vwap_slip_impact",
]


# ── Trade Logger ─────────────────────────────────────────────────────

class TradeLogger:
    """Appends closed trades to a CSV file and ensures headers exist."""

    def __init__(self, csv_path: Path = config.TRADES_CSV):
        self.csv_path = csv_path
        self._ensure_csv_headers()

    def _ensure_csv_headers(self) -> None:
        """Write CSV headers only if the file is new or empty (never wipe data)."""
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if self.csv_path.exists() and self.csv_path.stat().st_size > 0:
            logger.info("Appending to existing trade log CSV at %s", self.csv_path)
            return
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
        logger.info("Created trade log CSV at %s", self.csv_path)

    def log_restart_marker(self, n_open_positions: int = 0, capital: float = 0.0) -> None:
        """
        Write a clearly visible restart marker row in the CSV.
        Uses exit_reason='=== BOT RESTART ===' so it's unmissable in a spreadsheet.
        """
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        row = {col: "" for col in CSV_COLUMNS}
        row["timestamp_open"] = ts_now
        row["timestamp_close"] = ts_now
        row["market_question"] = f"=== BOT RESTART === open_pos={n_open_positions} capital=${capital:.2f}"
        row["exit_reason"] = "RESTART"
        row["capital_after"] = f"{capital:.4f}"
        row["pnl"] = "0.0000"
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writerow(row)
        logger.info("📌 Restart marker written to CSV.")

    def reset_csv(self) -> None:
        """Wipe the CSV and re-write headers.  Call at bot startup."""
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
        logger.info("Trades CSV reset (wiped) at startup: %s", self.csv_path)

    def snapshot_csv(self) -> Path | None:
        """
        Copy current trades CSV to trades_{timestamp}.csv as a session backup.
        Returns the snapshot path, or None if the CSV is empty/missing.
        """
        if not self.csv_path.exists() or self.csv_path.stat().st_size == 0:
            return None
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        snap_name = self.csv_path.stem + f"_{ts}" + self.csv_path.suffix
        snap_path = self.csv_path.parent / snap_name
        try:
            shutil.copy2(self.csv_path, snap_path)
            logger.info("📸 CSV snapshot saved: %s", snap_path.name)
            return snap_path
        except Exception as exc:
            logger.error("Error creating CSV snapshot: %s", exc)
            return None

    def log_trade(self, position: Position, capital_after: float) -> None:
        """Append a single closed trade to the CSV."""
        row = {
            "timestamp_open": position.entry_time,
            "timestamp_close": position.exit_time,
            "market_id": position.market_id,
            "market_question": position.market_question,
            "side": position.side.name,
            "entry_price": f"{position.entry_price:.6f}",
            "exit_price": f"{position.exit_price:.6f}" if position.exit_price else "",
            "shares": f"{position.size:.4f}",
            "cost": f"{position.cost:.4f}",
            "pnl": f"{position.pnl:.4f}",
            "exit_reason": position.exit_reason,
            "capital_after": f"{capital_after:.4f}",
            # v3 orderbook metrics
            "ob_imbalance": f"{position.ob_imbalance:.4f}",
            "yes_liquidity": f"{position.yes_liquidity:.2f}",
            "no_liquidity": f"{position.no_liquidity:.2f}",
            "entry_spread_pct": f"{position.entry_spread_pct:.6f}",
            # v3 data-science indicators
            "time_elapsed_s": f"{position.time_elapsed_s:.2f}",
            "spike_velocity": f"{position.spike_velocity:.6f}",
            "vwap_slip_impact": f"{position.vwap_slip_impact:.6f}",
        }
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writerow(row)
        logger.debug("Trade logged to CSV: PnL=%.4f, reason=%s", position.pnl, position.exit_reason)


# ── Application-wide logging setup ──────────────────────────────────

def setup_logging(level: str = config.LOG_LEVEL) -> None:
    """
    Configure root logger to output to both console and a log file.
    Call once at application startup.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Root logger
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Avoid duplicate handlers on repeated calls
    if root.handlers:
        return

    formatter = logging.Formatter(
        fmt="%(asctime)s  [%(levelname)-7s]  %(name)-25s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler – rotates at 20 MB, keeps 5 backups (≤ 100 MB total)
    # Appends to existing log on restart (backupCount preserves history)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        mode="a",
        maxBytes=20 * 1024 * 1024,   # 20 MB per file
        backupCount=5,
        encoding="utf-8",
        delay=False,
    )
    file_handler.setLevel(logging.DEBUG)  # always capture everything to file
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logger.info("Logging initialised (console=%s, file=%s).", level, config.LOG_FILE)
