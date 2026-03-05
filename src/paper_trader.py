"""
paper_trader.py - Hyper-realistic virtual capital manager (v5).

Every open and close applies REAL friction:
    • Entry  at best ASK + slippage  (worst-case taker buy)
    • Exit   at best BID - slippage  (worst-case taker sell)
    • Taker FEE deducted on both legs

v5 changes:
    • MULTIPLE open positions (dict keyed by token_id)
    • JSON persistence: state saved to data/state.json on every change
    • Restored on startup → survives restarts
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import config
from src.strategy import Side
from src.fees import calculate_dynamic_fee

logger = logging.getLogger(__name__)

# Default state file (overridden per-instance when label is provided)
DEFAULT_STATE_FILE = config.DATA_DIR / "state.json"


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class Position:
    """Tracks a single open paper-trade position."""
    market_id: str
    market_question: str
    token_id: str
    side: Side              # YES or NO
    entry_price: float      # effective entry (ask + slippage)
    raw_ask: float          # raw best-ask before friction
    size: float             # number of shares (units)
    cost: float             # total capital committed
    entry_fee: float        # fee deducted at open
    entry_slippage: float   # slippage cost at open
    entry_time: float = field(default_factory=time.time)
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    exit_fee: float = 0.0
    exit_slippage: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""
    # ── v3 orderbook metrics (logged per trade) ──
    ob_imbalance: float = 0.0       # bid/ask ratio at entry
    yes_liquidity: float = 0.0      # YES depth (top N levels)
    no_liquidity: float = 0.0       # NO  depth (top N levels)
    entry_spread_pct: float = 0.0   # spread at execution moment
    # ── v3 data-science indicators ──
    time_elapsed_s: float = 0.0     # secs from market open to entry
    spike_velocity: float = 0.0     # signed Δprice over 4 ticks
    vwap_slip_impact: float = 0.0   # VWAP_price - best_ask (real slippage cost)


@dataclass
class PortfolioSnapshot:
    """Summary of the current portfolio state."""
    available_capital: float
    total_pnl: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_fees: float
    total_slippage: float
    open_position: Optional[Position]


# ── Paper Trader ─────────────────────────────────────────────────────

class PaperTrader:
    """
    Manages virtual capital with institutional-grade friction model.
    Supports MULTIPLE open positions simultaneously (copy-trade needs this).
    Persists state to data/state.json.
    """

    def __init__(self, initial_capital: float = config.INITIAL_CAPITAL, label: str = ""):
        self.initial_capital = initial_capital
        self.available_capital = initial_capital
        self.label = label
        # Per-whale state file: data/state_DrPufferfish.json, etc.
        if label:
            self.state_file = config.DATA_DIR / f"state_{label}.json"
        else:
            self.state_file = DEFAULT_STATE_FILE
        self.open_positions: dict[str, Position] = {}  # keyed by token_id
        self.closed_trades: list[Position] = []
        # Friction parameters (fee is now dynamic – see src/fees.py)
        self.slippage_pct = config.SLIPPAGE_PCT

    # ── Backward compat: single open_position property ───────────
    @property
    def open_position(self) -> Optional[Position]:
        """Returns first open position (backward compat). Use open_positions dict."""
        if self.open_positions:
            return next(iter(self.open_positions.values()))
        return None

    # ── Open a position (buy at ASK + slippage + fee) ────────────

    def open_trade(
        self,
        market_id: str,
        market_question: str,
        token_id: str,
        side: Side,
        entry_price: float,        # best ASK from orderbook
        best_bid: float = 0.0,     # informational
        spread: float = 0.0,       # informational
        # v3 orderbook metrics
        ob_imbalance: float = 0.0,
        yes_liquidity: float = 0.0,
        no_liquidity: float = 0.0,
        entry_spread_pct: float = 0.0,
        orderbook_asks: list[dict] | None = None,  # for dynamic slippage
        # v3 data-science indicators
        time_elapsed_s: float = 0.0,
        spike_velocity: float = 0.0,
    ) -> Optional[Position]:
        """
        Simulate buying shares at the best ASK + slippage + fee.
        Supports multiple simultaneous positions (one per token_id).
        """
        if token_id in self.open_positions:
            logger.warning("Ya hay posición abierta para token %s…", token_id[:16])
            return None

        if self.available_capital <= 0:
            logger.warning("Cannot open trade – no capital available.")
            return None

        if entry_price <= 0 or entry_price >= 1:
            logger.warning(
                "Skipping trade – entry_price %.4f out of range (0,1).",
                entry_price,
            )
            return None

        # ── Determine allocation first (needed for dynamic slip) ──
        alloc = self.available_capital * config.POSITION_SIZE_PCT
        alloc = min(alloc, self.available_capital)  # safety cap
        if alloc < 0.01:
            logger.warning("Allocation %.4f too small. Skipping trade.", alloc)
            return None
        fee_rate = calculate_dynamic_fee(entry_price)
        fee = alloc * fee_rate
        net_alloc = alloc - fee
        if net_alloc <= 0:
            logger.warning("Fee eats entire allocation. Skipping trade.")
            return None

        # ── Apply friction (dynamic or static slippage) ───────
        if config.USE_DYNAMIC_SLIPPAGE and orderbook_asks:
            # Estimate how many shares we'd buy
            rough_shares = net_alloc / entry_price
            # Walk through the real orderbook
            from src.polymarket_api import OrderbookSnapshot
            dummy = OrderbookSnapshot(
                token_id=token_id, timestamp=0.0,
                best_ask=entry_price, asks=orderbook_asks,
            )
            vwap_buy = dummy.effective_buy_price(rough_shares)
            effective_entry = min(vwap_buy, 0.999)
            slippage_cost = effective_entry - entry_price
            logger.debug(
                "Dynamic slippage: VWAP=%.5f  best_ask=%.5f  slip=%.5f (%.2f%%)",
                vwap_buy, entry_price, slippage_cost,
                (slippage_cost / entry_price * 100) if entry_price > 0 else 0,
            )
        else:
            slippage_cost = entry_price * self.slippage_pct
            effective_entry = entry_price + slippage_cost
            effective_entry = min(effective_entry, 0.999)

        # Compute VWAP slip impact for data science
        vwap_slip = effective_entry - entry_price  # 0 when static, >0 when VWAP

        size = net_alloc / effective_entry

        pos = Position(
            market_id=market_id,
            market_question=market_question,
            token_id=token_id,
            side=side,
            entry_price=effective_entry,
            raw_ask=entry_price,
            size=size,
            cost=alloc,
            entry_fee=fee,
            entry_slippage=slippage_cost * size,
            ob_imbalance=ob_imbalance,
            yes_liquidity=yes_liquidity,
            no_liquidity=no_liquidity,
            entry_spread_pct=entry_spread_pct,
            time_elapsed_s=time_elapsed_s,
            spike_velocity=spike_velocity,
            vwap_slip_impact=vwap_slip,
        )

        self.available_capital -= alloc
        self.open_positions[token_id] = pos
        self._save_state()

        slip_label = "dyn" if (config.USE_DYNAMIC_SLIPPAGE and orderbook_asks) else "static"
        logger.info(
            "📝 PAPER OPEN  | %s | ask=%.4f → effective=%.4f "
            "(slip=%.4f [%s], fee=%.2f€ [%.2f%%]) | shares=%.2f | cost=%.2f€ "
            "| imb=%.2f yes_liq=%.0f no_liq=%.0f sprd=%.2f%%",
            side.name,
            entry_price,
            effective_entry,
            slippage_cost,
            slip_label,
            fee,
            fee_rate * 100,
            size,
            alloc,
            ob_imbalance,
            yes_liquidity,
            no_liquidity,
            entry_spread_pct * 100,
        )
        return pos

    # ── Close a position (sell at BID - slippage - fee) ──────────

    def close_trade(
        self,
        exit_price: float,      # best BID from orderbook (or limit price for maker)
        reason: str = "",
        is_maker: bool = False,  # True → limit fill, no slippage/fee
        token_id: str = "",      # which position to close (required for multi-pos)
    ) -> Optional[Position]:
        """
        Simulate selling a specific open position.
        If token_id is empty, closes the first (backward compat).
        """
        if token_id and token_id in self.open_positions:
            pos = self.open_positions[token_id]
        elif self.open_positions:
            # backward compat: close first position
            token_id = next(iter(self.open_positions))
            pos = self.open_positions[token_id]
        else:
            logger.warning("No open position to close.")
            return None
        pos.exit_time = time.time()
        pos.exit_reason = reason

        if is_maker:
            # ── Maker exit: no friction ───────────────────────
            effective_exit = exit_price
            pos.exit_price = effective_exit
            pos.exit_fee = 0.0
            pos.exit_slippage = 0.0
            slippage_cost = 0.0
        else:
            # ── Taker exit: apply friction ────────────────────
            slippage_cost = exit_price * self.slippage_pct
            effective_exit = exit_price - slippage_cost
            effective_exit = max(effective_exit, 0.001)
            pos.exit_price = effective_exit
            exit_fee_rate = calculate_dynamic_fee(exit_price)
            exit_fee = (effective_exit * pos.size) * exit_fee_rate
            pos.exit_fee = exit_fee
            pos.exit_slippage = slippage_cost * pos.size

        # PnL = (exit - entry) * shares - exit_fee
        pos.pnl = (effective_exit - pos.entry_price) * pos.size - pos.exit_fee

        # Return capital: original cost + PnL
        proceeds = pos.cost + pos.pnl
        self.available_capital += max(proceeds, 0.0)

        self.closed_trades.append(pos)
        del self.open_positions[token_id]
        self._save_state()

        total_friction = pos.entry_fee + pos.exit_fee + pos.entry_slippage + pos.exit_slippage
        exit_label = "MAKER" if is_maker else "TAKER"
        emoji = "💰" if pos.pnl >= 0 else "💸"
        logger.info(
            "%s PAPER CLOSE [%s] | %s | entry=%.4f → exit=%.4f (raw=%.4f) "
            "| PnL=%.2f€ | friction=%.2f€ (fees=%.2f€ + slip=%.2f€) "
            "| reason=%s | capital=%.2f€",
            emoji,
            exit_label,
            pos.side.name,
            pos.entry_price,
            effective_exit,
            exit_price,
            pos.pnl,
            total_friction,
            pos.entry_fee + pos.exit_fee,
            pos.entry_slippage + pos.exit_slippage,
            reason,
            self.available_capital,
        )
        return pos

    # ── Partial close (proportional sell) ─────────────────────────

    def partial_close_trade(
        self,
        token_id: str,
        fraction: float,           # 0.0 – 1.0  (proportion of shares to sell)
        exit_price: float,
        reason: str = "",
        is_maker: bool = False,
    ) -> Optional[Position]:
        """
        Sell *fraction* of an open position, keeping the remainder open.

        If fraction >= 1.0 (or remaining shares would be dust), delegates to
        full close_trade().

        Returns a synthetic Position representing the *closed slice* (for CSV
        logging), or None on error.  The original Position stays in
        open_positions with reduced size/cost.
        """
        if token_id not in self.open_positions:
            logger.warning("partial_close: no open position for token %s…", token_id[:16])
            return None

        pos = self.open_positions[token_id]
        fraction = max(0.0, min(fraction, 1.0))

        # If fraction is essentially full → delegate to close_trade
        if fraction >= 0.99 or pos.size * (1 - fraction) < 0.01:
            return self.close_trade(
                exit_price=exit_price, reason=reason,
                is_maker=is_maker, token_id=token_id,
            )

        sold_shares = pos.size * fraction
        sold_cost   = pos.cost * fraction

        # ── Friction ──────────────────────────────────────────
        if is_maker:
            effective_exit = exit_price
            exit_fee = 0.0
            exit_slippage = 0.0
        else:
            slippage_cost = exit_price * self.slippage_pct
            effective_exit = max(exit_price - slippage_cost, 0.001)
            exit_fee_rate = calculate_dynamic_fee(exit_price)
            exit_fee = (effective_exit * sold_shares) * exit_fee_rate
            exit_slippage = slippage_cost * sold_shares

        # PnL for the sold slice
        pnl_slice = (effective_exit - pos.entry_price) * sold_shares - exit_fee
        proceeds  = sold_cost + pnl_slice
        self.available_capital += max(proceeds, 0.0)

        # ── Build a synthetic closed Position for CSV logging ─
        closed_slice = Position(
            market_id=pos.market_id,
            market_question=pos.market_question,
            token_id=pos.token_id,
            side=pos.side,
            entry_price=pos.entry_price,
            raw_ask=pos.raw_ask,
            size=sold_shares,
            cost=sold_cost,
            entry_fee=pos.entry_fee * fraction,
            entry_slippage=pos.entry_slippage * fraction,
            entry_time=pos.entry_time,
            exit_price=effective_exit,
            exit_time=time.time(),
            exit_fee=exit_fee,
            exit_slippage=exit_slippage if not is_maker else 0.0,
            pnl=pnl_slice,
            exit_reason=reason,
            ob_imbalance=pos.ob_imbalance,
            yes_liquidity=pos.yes_liquidity,
            no_liquidity=pos.no_liquidity,
            entry_spread_pct=pos.entry_spread_pct,
            time_elapsed_s=pos.time_elapsed_s,
            spike_velocity=pos.spike_velocity,
            vwap_slip_impact=pos.vwap_slip_impact,
        )
        self.closed_trades.append(closed_slice)

        # ── Shrink the remaining position in place ────────────
        pos.size -= sold_shares
        pos.cost -= sold_cost
        pos.entry_fee   *= (1 - fraction)
        pos.entry_slippage *= (1 - fraction)
        self._save_state()

        emoji = "\U0001f4b0" if pnl_slice >= 0 else "\U0001f4b8"  # 💰 / 💸
        logger.info(
            "%s PAPER PARTIAL CLOSE [%.0f%%] | %s | entry=%.4f → exit=%.4f "
            "| PnL=%.2f | sold_shares=%.2f / remaining=%.2f "
            "| reason=%s | capital=%.2f",
            emoji,
            fraction * 100,
            pos.side.name,
            pos.entry_price,
            effective_exit,
            pnl_slice,
            sold_shares,
            pos.size,
            reason,
            self.available_capital,
        )
        return closed_slice

    # ── Portfolio stats ──────────────────────────────────────────

    def get_snapshot(self) -> PortfolioSnapshot:
        wins = sum(1 for t in self.closed_trades if t.pnl > 0)
        losses = sum(1 for t in self.closed_trades if t.pnl <= 0)
        total = len(self.closed_trades)
        total_pnl = sum(t.pnl for t in self.closed_trades)
        total_fees = sum(t.entry_fee + t.exit_fee for t in self.closed_trades)
        total_slippage = sum(
            t.entry_slippage + t.exit_slippage for t in self.closed_trades
        )

        return PortfolioSnapshot(
            available_capital=self.available_capital,
            total_pnl=total_pnl,
            total_trades=total,
            winning_trades=wins,
            losing_trades=losses,
            win_rate=(wins / total * 100) if total > 0 else 0.0,
            total_fees=total_fees,
            total_slippage=total_slippage,
            open_position=self.open_position,
        )

    def print_summary(self) -> None:
        """Pretty-print a portfolio summary to the console."""
        snap = self.get_snapshot()
        print("\n" + "=" * 62)
        print("  📊  PAPER TRADING SUMMARY  (realistic friction)")
        print("=" * 62)
        print(f"  Initial capital : {self.initial_capital:.2f} €")
        print(f"  Current capital : {snap.available_capital:.2f} €")
        print(f"  Total PnL       : {snap.total_pnl:+.2f} €")
        print(f"  Total trades    : {snap.total_trades}")
        print(f"  Wins / Losses   : {snap.winning_trades} / {snap.losing_trades}")
        print(f"  Win rate        : {snap.win_rate:.1f} %")
        print(f"  ── Friction breakdown ──")
        print(f"  Total fees      : {snap.total_fees:.2f} €  "
              f"(dynamic curve × 2 legs)")
        print(f"  Total slippage  : {snap.total_slippage:.2f} €  "
              f"(slip {self.slippage_pct:.1%} × 2 legs)")
        print(f"  Total friction  : {snap.total_fees + snap.total_slippage:.2f} €")
        if snap.open_position:
            print(f"  Open positions  : {len(self.open_positions)}")
            for tid, p in self.open_positions.items():
                print(f"    • {p.side.name} {p.market_question[:40]} "
                      f"@ {p.entry_price:.4f} (${p.cost:.2f})")
        else:
            print("  Open positions  : None")
        print("=" * 62 + "\n")

    # ── Persistence ──────────────────────────────────────────────

    def _save_state(self) -> None:
        """Persist open positions + capital to JSON (called after every change)."""
        try:
            positions_data = {}
            for tid, pos in self.open_positions.items():
                positions_data[tid] = {
                    "market_id": pos.market_id,
                    "market_question": pos.market_question,
                    "token_id": pos.token_id,
                    "side": pos.side.name,
                    "entry_price": pos.entry_price,
                    "raw_ask": pos.raw_ask,
                    "size": pos.size,
                    "cost": pos.cost,
                    "entry_fee": pos.entry_fee,
                    "entry_slippage": pos.entry_slippage,
                    "entry_time": pos.entry_time,
                    "ob_imbalance": pos.ob_imbalance,
                    "yes_liquidity": pos.yes_liquidity,
                    "no_liquidity": pos.no_liquidity,
                    "entry_spread_pct": pos.entry_spread_pct,
                    "time_elapsed_s": pos.time_elapsed_s,
                    "spike_velocity": pos.spike_velocity,
                    "vwap_slip_impact": pos.vwap_slip_impact,
                }

            state = {
                "available_capital": self.available_capital,
                "initial_capital": self.initial_capital,
                "total_closed": len(self.closed_trades),
                "total_pnl": sum(t.pnl for t in self.closed_trades),
                "open_positions": positions_data,
                "saved_at": time.time(),
            }
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            tmp.replace(self.state_file)  # atomic on most OS
            logger.debug("💾 State saved: %d open positions, $%.2f capital",
                         len(positions_data), self.available_capital)
        except Exception as exc:
            logger.error("Error saving state: %s", exc)

    def load_state(self) -> bool:
        """
        Load persisted state from JSON. Returns True if state was restored.
        Call ONCE at startup before the main loop.
        """
        if not self.state_file.exists():
            logger.info("No saved state found (%s). Starting fresh.", self.state_file.name)
            return False

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)

            self.available_capital = state.get("available_capital", self.initial_capital)
            positions_data = state.get("open_positions", {})

            for tid, pd in positions_data.items():
                side = Side.YES if pd["side"] == "YES" else Side.NO
                pos = Position(
                    market_id=pd["market_id"],
                    market_question=pd["market_question"],
                    token_id=pd["token_id"],
                    side=side,
                    entry_price=pd["entry_price"],
                    raw_ask=pd["raw_ask"],
                    size=pd["size"],
                    cost=pd["cost"],
                    entry_fee=pd["entry_fee"],
                    entry_slippage=pd["entry_slippage"],
                    entry_time=pd.get("entry_time", time.time()),
                    ob_imbalance=pd.get("ob_imbalance", 0.0),
                    yes_liquidity=pd.get("yes_liquidity", 0.0),
                    no_liquidity=pd.get("no_liquidity", 0.0),
                    entry_spread_pct=pd.get("entry_spread_pct", 0.0),
                    time_elapsed_s=pd.get("time_elapsed_s", 0.0),
                    spike_velocity=pd.get("spike_velocity", 0.0),
                    vwap_slip_impact=pd.get("vwap_slip_impact", 0.0),
                )
                self.open_positions[tid] = pos

            saved_at = state.get("saved_at", 0)
            age_min = (time.time() - saved_at) / 60 if saved_at else 0
            logger.info(
                "✅ State restored: %d open positions, $%.2f capital "
                "(saved %.0f min ago)",
                len(self.open_positions), self.available_capital, age_min,
            )
            return True

        except Exception as exc:
            logger.error("Error loading state: %s – starting fresh.", exc)
            return False
