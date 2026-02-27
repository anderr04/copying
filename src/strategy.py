"""
strategy.py  –  Momentum / Trend Following v7
Multi-timeframe BTC binary markets on Polymarket.

v7 – Multi-Timeframe momentum
-------------------------------
Core logic UNCHANGED from v6.  The parameters are now loaded from
per-timeframe profiles in config.py (daily / 4h / 15m / 5m).

Daily markets give R:R ≈ 5:1, break-even WR ~16%.
Spike detection + momentum exit engine works at any timeframe.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import config
from src.polymarket_api import PolymarketClient, Market, Token, OrderbookSnapshot
from src.fees import calculate_dynamic_fee

logger = logging.getLogger(__name__)


# ── Enums & data classes ─────────────────────────────────────────────

class Side(Enum):
    YES = auto()
    NO = auto()


class Signal(Enum):
    WAIT = auto()
    BUY_YES = auto()
    BUY_NO = auto()
    NO_TRADE = auto()


@dataclass
class TradeIdea:
    """Encapsulates an entry signal emitted by the strategy."""
    signal: Signal
    market: Market
    token: Token
    side: Side
    entry_price: float          # best_ask at moment of signal
    best_bid: float = 0.0
    spread: float = 0.0
    spread_pct: float = 0.0
    mid_price: float = 0.0
    ev_net: float = 0.0
    reject_reason: str = ""
    timestamp: float = field(default_factory=time.time)
    ob_imbalance: float = 0.0
    yes_liquidity: float = 0.0
    no_liquidity: float = 0.0
    target_asks: list[dict] = field(default_factory=list)
    time_elapsed_s: float = 0.0
    spike_velocity: float = 0.0


@dataclass
class PriceHistory:
    """Rolling price samples captured during observation."""
    timestamps: list[float] = field(default_factory=list)
    prices: list[float] = field(default_factory=list)

    def add(self, ts: float, price: float) -> None:
        self.timestamps.append(ts)
        self.prices.append(price)

    @property
    def first_price(self) -> Optional[float]:
        return self.prices[0] if self.prices else None

    @property
    def last_price(self) -> Optional[float]:
        return self.prices[-1] if self.prices else None

    @property
    def price_change(self) -> float:
        if len(self.prices) < 2:
            return 0.0
        return self.prices[-1] - self.prices[0]

    def rolling_average(self, window: int = 8) -> Optional[float]:
        if len(self.prices) < window:
            return None
        return sum(self.prices[-window:]) / window

    def reset(self) -> None:
        self.timestamps.clear()
        self.prices.clear()


# ── Momentum Strategy Engine ─────────────────────────────────────────

class MomentumStrategy:
    """
    Stateful strategy:
        wait → observe → detect spike → confirm persistence → enter WITH trend.
    Position exit is driven by momentum exhaustion, not static TP/SL.
    """

    def __init__(self, client: PolymarketClient):
        self.client = client
        self.history = PriceHistory()

        # ── Observation & entry thresholds ────────────────────
        self.wait_seconds: int = config.WAIT_SECONDS
        self.rolling_window: int = config.ROLLING_WINDOW
        self.spike_deviation: float = config.SPIKE_DEVIATION
        self.min_residual_deviation: float = config.MIN_RESIDUAL_DEVIATION
        self.poll_interval: float = config.POLL_INTERVAL
        self.max_entry_price: float = config.MAX_ENTRY_PRICE
        self.post_trade_cooldown_s: float = config.POST_TRADE_COOLDOWN_S
        self.min_time_elapsed_s: float = config.MIN_TIME_ELAPSED_S
        self.min_ob_imbalance: float = config.MIN_OB_IMBALANCE
        self.max_ob_imbalance: float = config.MAX_OB_IMBALANCE
        self.min_spike_velocity_abs: float = config.MIN_SPIKE_VELOCITY_ABS

        # ── Friction ──────────────────────────────────────────
        self.slippage_pct: float = config.SLIPPAGE_PCT
        self.max_spread: float = config.MAX_SPREAD

        # ── Momentum exit parameters ─────────────────────────
        self.trailing_activation: float = config.TRAILING_ACTIVATION
        self.trailing_distance: float = config.TRAILING_DISTANCE
        self.velocity_window: int = config.VELOCITY_WINDOW
        self.stall_threshold: float = config.STALL_VELOCITY_THRESHOLD
        self.stall_ticks: int = config.STALL_TICKS_TO_EXIT
        self.accel_threshold: float = config.ACCEL_EXIT_THRESHOLD
        self.spread_emergency: float = config.SPREAD_EMERGENCY
        self.ob_collapse_threshold: float = config.OB_COLLAPSE_THRESHOLD
        self.hard_stop_loss_usd: float = config.HARD_STOP_LOSS_USD
        self.hard_stop_loss_pct: float = config.HARD_STOP_LOSS_PCT
        self.time_stop_before_close: int = config.TIME_STOP_BEFORE_CLOSE
        self.max_hold_seconds: int = config.MAX_HOLD_SECONDS

        # ── Internal observation state ────────────────────────
        self._market_open_ts: Optional[float] = None
        self._last_trade_close_ts: float = 0.0

        # ── Armed (spike confirmation) state ──────────────────
        self._armed: bool = False
        self._armed_tick: int = 0
        self._armed_token: Optional[Token] = None
        self._armed_side: Optional[Side] = None
        self._armed_baseline_avg: float = 0.0
        self._armed_spike_mid: float = 0.0
        self._armed_spike_velocity: float = 0.0
        self._armed_label: str = ""

        # ── Display state ─────────────────────────────────────
        self._last_deviation: float = 0.0
        self._last_rolling_avg: float = 0.0

        # ── Position monitoring state (momentum exit) ─────────
        self._pos_prices: list[tuple[float, float]] = []
        self._trailing_peak: float = 0.0
        self._stall_count: int = 0

    # ── Properties for console display ───────────────────────────

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def current_deviation(self) -> float:
        return self._last_deviation

    @property
    def current_rolling_avg(self) -> float:
        return self._last_rolling_avg

    @property
    def trailing_peak(self) -> float:
        return self._trailing_peak

    @property
    def in_cooldown(self) -> bool:
        if self._last_trade_close_ts <= 0:
            return False
        return (time.time() - self._last_trade_close_ts) < self.post_trade_cooldown_s

    def notify_trade_closed(self) -> None:
        """Called by main.py after a trade is closed to start cooldown."""
        self._last_trade_close_ts = time.time()
        self._disarm()
        self._pos_prices.clear()
        self._trailing_peak = 0.0
        self._stall_count = 0
        logger.info(
            "Post-trade cooldown started (%.0f s).",
            self.post_trade_cooldown_s,
        )

    # ── Armed-state helpers ──────────────────────────────────────

    def _arm(
        self,
        token: Token,
        side: Side,
        baseline_avg: float,
        spike_mid: float,
        label: str,
        spike_velocity: float = 0.0,
    ) -> None:
        self._armed = True
        self._armed_tick = 0
        self._armed_token = token
        self._armed_side = side
        self._armed_baseline_avg = baseline_avg
        self._armed_spike_mid = spike_mid
        self._armed_spike_velocity = spike_velocity
        self._armed_label = label

    def _disarm(self) -> None:
        self._armed = False
        self._armed_tick = 0
        self._armed_token = None
        self._armed_side = None
        self._armed_baseline_avg = 0.0
        self._armed_spike_mid = 0.0
        self._armed_spike_velocity = 0.0
        self._armed_label = ""

    # ── EV Gate (friction check) ─────────────────────────────────

    def compute_ev(
        self, best_ask: float, best_bid: float, spread: float,
    ) -> tuple[float, str]:
        """
        Quick EV sanity check: validates spread isn't too wide and
        friction doesn't eat the entire expected move.
        """
        mid = (best_ask + best_bid) / 2.0
        spread_pct = (spread / mid) if mid > 0 else 1.0
        if spread_pct > self.max_spread:
            return (
                -spread_pct,
                f"Spread {spread_pct:.2%} > max {self.max_spread:.2%}",
            )

        # Entry cost (taker)
        entry_fee_rate = calculate_dynamic_fee(best_ask)
        entry_slip = best_ask * self.slippage_pct
        eff_entry = min(best_ask + entry_slip, 0.999)
        entry_fee = eff_entry * entry_fee_rate

        # For momentum: assume a conservative +5% move in our favour.
        expected_move = 0.05
        target = min(best_ask + expected_move, 0.99)
        exit_fee_rate = calculate_dynamic_fee(target)
        exit_slip = target * self.slippage_pct
        total_friction = entry_fee + target * exit_fee_rate + entry_slip + exit_slip
        net_ev = expected_move - total_friction
        if net_ev <= 0:
            return (
                net_ev,
                f"EV={net_ev:+.4f} ≤ 0 (friction too high at ask={best_ask:.4f})",
            )
        return (net_ev, "")

    # ── Armed evaluation (confirm spike persists) ────────────────

    def _evaluate_armed(
        self,
        market: Market,
        yes_token: Token,
        yes_snap: OrderbookSnapshot,
    ) -> TradeIdea:
        """
        One tick during spike-confirmation cooldown.
        After cooldown: enter WITH trend if deviation persists, else cancel.
        """
        self._armed_tick += 1
        current_mid = yes_snap.mid_price
        residual_dev = current_mid - self._armed_baseline_avg

        armed_token = self._armed_token or yes_token
        armed_side = self._armed_side or Side.YES

        # ── Still cooling down ───────────────────────────────
        if self._armed_tick < config.ARMED_COOLDOWN_TICKS:
            logger.info(
                "🔒 CONFIRM [%d/%d]  mid=%.4f  avg=%.4f  "
                "residual=%+.4f (need ≥ %.4f)",
                self._armed_tick,
                config.ARMED_COOLDOWN_TICKS,
                current_mid,
                self._armed_baseline_avg,
                residual_dev,
                self.min_residual_deviation,
            )
            return TradeIdea(
                signal=Signal.WAIT,
                market=market,
                token=armed_token,
                side=armed_side,
                entry_price=yes_snap.best_ask,
                best_bid=yes_snap.best_bid,
                spread=yes_snap.spread,
                spread_pct=yes_snap.spread_pct,
                mid_price=current_mid,
            )

        # ── Cooldown complete ────────────────────────────────

        # Spike evaporated?
        if abs(residual_dev) < self.min_residual_deviation:
            logger.info(
                "❌ SPIKE EVAPORATED: residual=%.4f < %.4f. Cancel.",
                abs(residual_dev), self.min_residual_deviation,
            )
            print(
                f"\n  ❌ Spike desvanecido (residual {abs(residual_dev):.4f} "
                f"< {self.min_residual_deviation})"
            )
            self._disarm()
            return TradeIdea(
                signal=Signal.WAIT,
                market=market,
                token=yes_token,
                side=Side.YES,
                entry_price=yes_snap.best_ask,
                best_bid=yes_snap.best_bid,
                spread=yes_snap.spread,
                spread_pct=yes_snap.spread_pct,
                mid_price=current_mid,
            )

        # ── Poll the target (momentum) token ─────────────────
        if armed_token.token_id != yes_token.token_id:
            target_snap = self.client.poll_orderbook(armed_token.token_id)
        else:
            target_snap = yes_snap

        # ── EV gate ──────────────────────────────────────────
        ev_net, reject = self.compute_ev(
            target_snap.best_ask, target_snap.best_bid, target_snap.spread,
        )
        if reject:
            logger.info("❌ Spike persists but EV negative: %s", reject)
            print(f"\n  ❌ Spike OK pero EV negativo: {reject}")
            self._disarm()
            return TradeIdea(
                signal=Signal.NO_TRADE,
                market=market,
                token=armed_token,
                side=armed_side,
                entry_price=target_snap.best_ask,
                best_bid=target_snap.best_bid,
                spread=target_snap.spread,
                spread_pct=target_snap.spread_pct,
                mid_price=target_snap.mid_price,
                ev_net=ev_net,
                reject_reason=reject,
            )

        # ── MAX ENTRY PRICE filter ───────────────────────────
        if target_snap.best_ask > self.max_entry_price:
            reason = (
                f"Ask {target_snap.best_ask:.4f} > max "
                f"{self.max_entry_price:.2f}"
            )
            logger.info("❌ ENTRY TOO EXPENSIVE: %s", reason)
            print(f"\n  ❌ Entrada cara: {reason}")
            self._disarm()
            return TradeIdea(
                signal=Signal.NO_TRADE,
                market=market,
                token=armed_token,
                side=armed_side,
                entry_price=target_snap.best_ask,
                best_bid=target_snap.best_bid,
                spread=target_snap.spread,
                spread_pct=target_snap.spread_pct,
                mid_price=target_snap.mid_price,
                ev_net=ev_net,
                reject_reason=reason,
            )

        # ── Orderbook metrics ────────────────────────────────
        imb_levels = config.IMBALANCE_LEVELS
        entry_imbalance = target_snap.imbalance(imb_levels)

        if armed_side == Side.NO:
            yes_liq = yes_snap.total_depth(imb_levels)
            no_liq = target_snap.total_depth(imb_levels)
        else:
            yes_liq = target_snap.total_depth(imb_levels)
            no_tok = self._get_no_token(market)
            if no_tok:
                no_snap_aux = self.client.poll_orderbook(no_tok.token_id)
                no_liq = no_snap_aux.total_depth(imb_levels)
            else:
                no_liq = 0.0

        # ── OB imbalance filter ──────────────────────────────
        if entry_imbalance < self.min_ob_imbalance:
            reason = (
                f"OB imbalance {entry_imbalance:.3f} < min "
                f"{self.min_ob_imbalance:.2f} (weak support)"
            )
            logger.info("❌ WEAK ORDERBOOK: %s", reason)
            print(f"\n  ❌ Orderbook débil: {reason}")
            self._disarm()
            return TradeIdea(
                signal=Signal.NO_TRADE,
                market=market,
                token=armed_token,
                side=armed_side,
                entry_price=target_snap.best_ask,
                best_bid=target_snap.best_bid,
                spread=target_snap.spread,
                spread_pct=target_snap.spread_pct,
                mid_price=target_snap.mid_price,
                ev_net=ev_net,
                ob_imbalance=entry_imbalance,
                reject_reason=reason,
            )

        # ── OB imbalance upper bound (extreme = illiquid) ────
        if entry_imbalance > self.max_ob_imbalance:
            reason = (
                f"OB imbalance {entry_imbalance:.3f} > max "
                f"{self.max_ob_imbalance:.2f} (extreme/illiquid)"
            )
            logger.info("❌ EXTREME ORDERBOOK: %s", reason)
            print(f"\n  ❌ Orderbook extremo: {reason}")
            self._disarm()
            return TradeIdea(
                signal=Signal.NO_TRADE,
                market=market,
                token=armed_token,
                side=armed_side,
                entry_price=target_snap.best_ask,
                best_bid=target_snap.best_bid,
                spread=target_snap.spread,
                spread_pct=target_snap.spread_pct,
                mid_price=target_snap.mid_price,
                ev_net=ev_net,
                ob_imbalance=entry_imbalance,
                reject_reason=reason,
            )

        # ── Spike velocity filter ────────────────────────────
        if abs(self._armed_spike_velocity) < self.min_spike_velocity_abs:
            reason = (
                f"|spike_vel|={abs(self._armed_spike_velocity):.4f} "
                f"< min {self.min_spike_velocity_abs:.4f} (weak spike)"
            )
            logger.info("❌ WEAK SPIKE: %s", reason)
            print(f"\n  ❌ Spike débil: {reason}")
            self._disarm()
            return TradeIdea(
                signal=Signal.NO_TRADE,
                market=market,
                token=armed_token,
                side=armed_side,
                entry_price=target_snap.best_ask,
                best_bid=target_snap.best_bid,
                spread=target_snap.spread,
                spread_pct=target_snap.spread_pct,
                mid_price=target_snap.mid_price,
                ev_net=ev_net,
                ob_imbalance=entry_imbalance,
                reject_reason=reason,
            )

        # ── CONFIRMED MOMENTUM ENTRY ─────────────────────────
        sig = Signal.BUY_YES if armed_side == Side.YES else Signal.BUY_NO

        t_elapsed = (time.time() - self._market_open_ts) if self._market_open_ts else 0.0
        s_velocity = self._armed_spike_velocity

        logger.info(
            "✅ MOMENTUM CONFIRMED: %s  residual=%+.4f  "
            "EV=%.4f  entry ask=%.4f  imb=%.2f  yes_liq=%.1f  no_liq=%.1f  "
            "t_elapsed=%.1f  spike_vel=%+.4f",
            self._armed_label,
            residual_dev,
            ev_net,
            target_snap.best_ask,
            entry_imbalance,
            yes_liq,
            no_liq,
            t_elapsed,
            s_velocity,
        )
        print(
            f"\n  ✅ MOMENTUM ENTRY: {self._armed_label}  "
            f"(residual={abs(residual_dev):.4f}, EV={ev_net:.4f}, "
            f"imb={entry_imbalance:.2f})"
        )
        result = TradeIdea(
            signal=sig,
            market=market,
            token=armed_token,
            side=armed_side,
            entry_price=target_snap.best_ask,
            best_bid=target_snap.best_bid,
            spread=target_snap.spread,
            spread_pct=target_snap.spread_pct,
            mid_price=target_snap.mid_price,
            ev_net=ev_net,
            ob_imbalance=entry_imbalance,
            yes_liquidity=yes_liq,
            no_liquidity=no_liq,
            target_asks=list(target_snap.asks),
            time_elapsed_s=t_elapsed,
            spike_velocity=s_velocity,
        )
        self._disarm()
        return result

    # ── Phase 1: Wait & Observe ──────────────────────────────────

    def begin_observation(self, market_open_ts: float) -> None:
        self._market_open_ts = market_open_ts
        self.history.reset()
        self._disarm()
        self._last_deviation = 0.0
        self._last_rolling_avg = 0.0
        logger.info(
            "Observation started.  Baseline=%d s, rolling_window=%d.",
            self.wait_seconds,
            self.rolling_window,
        )

    def evaluate(self, market: Market) -> TradeIdea:
        """
        Called every tick during Phase A (entry detection).
        Detects directional spikes and signals to enter WITH the trend.
        """
        now = time.time()
        _empty_tok = Token("", "")

        if self._market_open_ts is None:
            return TradeIdea(
                signal=Signal.NO_TRADE, market=market,
                token=market.tokens[0] if market.tokens else _empty_tok,
                side=Side.YES, entry_price=0.0,
            )

        elapsed = now - self._market_open_ts

        # ── Still in wait phase? ─────────────────────────────
        if elapsed < self.wait_seconds:
            yes_token = self._get_yes_token(market)
            if yes_token:
                snap = self.client.poll_orderbook(yes_token.token_id)
                self.history.add(now, snap.mid_price)
            return TradeIdea(
                signal=Signal.WAIT, market=market,
                token=market.tokens[0] if market.tokens else _empty_tok,
                side=Side.YES, entry_price=0.0,
            )

        # ── Fetch orderbook ──────────────────────────────────
        yes_token = self._get_yes_token(market)
        no_token = self._get_no_token(market)
        if yes_token is None:
            logger.warning("No YES token in market %s", market.condition_id)
            return TradeIdea(
                signal=Signal.NO_TRADE, market=market,
                token=_empty_tok, side=Side.YES, entry_price=0.0,
            )

        yes_snap = self.client.poll_orderbook(yes_token.token_id)
        current_mid = yes_snap.mid_price
        self.history.add(now, current_mid)

        # ── Post-trade cooldown ──────────────────────────────
        if self.in_cooldown:
            rolling_avg = self.history.rolling_average(self.rolling_window)
            if rolling_avg is not None:
                self._last_deviation = current_mid - rolling_avg
                self._last_rolling_avg = rolling_avg
            return TradeIdea(
                signal=Signal.WAIT, market=market,
                token=yes_token, side=Side.YES,
                entry_price=yes_snap.best_ask,
                best_bid=yes_snap.best_bid,
                spread=yes_snap.spread,
                spread_pct=yes_snap.spread_pct,
                mid_price=current_mid,
            )

        # ── Compute rolling average & deviation ──────────────
        rolling_avg = self.history.rolling_average(self.rolling_window)
        if rolling_avg is not None:
            deviation = current_mid - rolling_avg
        else:
            deviation = 0.0
            rolling_avg = current_mid

        self._last_deviation = deviation
        self._last_rolling_avg = rolling_avg

        logger.debug(
            "Sample #%d  mid=%.4f  avg=%.4f  dev=%+.4f  "
            "bid=%.4f  ask=%.4f  sprd=%.2f%%",
            len(self.history.prices), current_mid, rolling_avg, deviation,
            yes_snap.best_bid, yes_snap.best_ask,
            yes_snap.spread_pct * 100,
        )

        # ── If armed → delegate to confirmation handler ──────
        if self._armed:
            return self._evaluate_armed(market, yes_token, yes_snap)

        # ── Need enough samples ──────────────────────────────
        if self.history.rolling_average(self.rolling_window) is None:
            return TradeIdea(
                signal=Signal.WAIT, market=market,
                token=yes_token, side=Side.YES,
                entry_price=yes_snap.best_ask,
                best_bid=yes_snap.best_bid,
                spread=yes_snap.spread,
                spread_pct=yes_snap.spread_pct,
                mid_price=current_mid,
            )

        # ── Spike detection ──────────────────────────────────
        if abs(deviation) >= self.spike_deviation:

            # Ignore early spikes
            if elapsed < self.min_time_elapsed_s:
                logger.info(
                    "⏳ SPIKE IGNORED (early): dev=%+.4f but "
                    "elapsed=%.1f s < min %.1f s.",
                    deviation, elapsed, self.min_time_elapsed_s,
                )
                return TradeIdea(
                    signal=Signal.WAIT, market=market,
                    token=yes_token, side=Side.YES,
                    entry_price=yes_snap.best_ask,
                    best_bid=yes_snap.best_bid,
                    spread=yes_snap.spread,
                    spread_pct=yes_snap.spread_pct,
                    mid_price=current_mid,
                )

            # ── MOMENTUM: buy WITH the direction ─────────────
            if deviation > 0:
                # YES spiking UP → BUY YES (ride upward momentum)
                momentum_token = yes_token
                momentum_side = Side.YES
                label = f"📈 UP +{deviation:.4f} → Buy YES (momentum)"
            else:
                # YES dipping DOWN → BUY NO (ride downward momentum)
                momentum_token = no_token if no_token else yes_token
                momentum_side = Side.NO
                label = f"📉 DN {deviation:+.4f} → Buy NO (momentum)"

            logger.info(
                "🔒 SPIKE DETECTED: %s  (mid=%.4f, avg=%.4f, dev=%+.4f). "
                "Arming cooldown %d ticks.",
                label, current_mid, rolling_avg, deviation,
                config.ARMED_COOLDOWN_TICKS,
            )
            print(
                f"\n  🔒 SPIKE detectado (dev={deviation:+.4f}, "
                f"avg={rolling_avg:.4f}). Cooldown "
                f"{config.ARMED_COOLDOWN_TICKS} ticks…"
            )

            # Spike velocity: signed Δprice over last 4 ticks
            if len(self.history.prices) >= 5:
                _sv = self.history.prices[-1] - self.history.prices[-5]
            elif len(self.history.prices) >= 2:
                _sv = self.history.prices[-1] - self.history.prices[-2]
            else:
                _sv = deviation

            self._arm(
                token=momentum_token,
                side=momentum_side,
                baseline_avg=rolling_avg,
                spike_mid=current_mid,
                label=label,
                spike_velocity=_sv,
            )
            return TradeIdea(
                signal=Signal.WAIT, market=market,
                token=momentum_token, side=momentum_side,
                entry_price=yes_snap.best_ask,
                best_bid=yes_snap.best_bid,
                spread=yes_snap.spread,
                spread_pct=yes_snap.spread_pct,
                mid_price=current_mid,
            )

        # ── No spike yet → keep observing ────────────────────
        return TradeIdea(
            signal=Signal.WAIT, market=market,
            token=yes_token, side=Side.YES,
            entry_price=yes_snap.best_ask,
            best_bid=yes_snap.best_bid,
            spread=yes_snap.spread,
            spread_pct=yes_snap.spread_pct,
            mid_price=current_mid,
        )

    # ── Phase 2: Position Monitoring (Momentum Exit Engine) ──────

    def begin_position_monitoring(self, entry_price: float) -> None:
        """Called when a position is opened. Resets momentum tracking."""
        self._pos_prices = [(time.time(), entry_price)]
        self._trailing_peak = entry_price
        self._stall_count = 0
        logger.info(
            "Position monitoring started: entry=%.4f, "
            "trail_act=%.4f, trail_dist=%.4f",
            entry_price, self.trailing_activation, self.trailing_distance,
        )

    def compute_velocity(self) -> Optional[float]:
        """
        dP/dt over the last velocity_window samples.
        Returns: Δprice per second (positive = price rising).
        """
        n = min(self.velocity_window, len(self._pos_prices))
        if n < 2:
            return None
        recent = self._pos_prices[-n:]
        dt = recent[-1][0] - recent[0][0]
        if dt <= 0:
            return None
        return (recent[-1][1] - recent[0][1]) / dt

    def compute_acceleration(self) -> Optional[float]:
        """
        d²P/dt² — change in velocity over 2× velocity_window samples.
        Returns: Δvelocity per second (negative = decelerating).
        """
        n = self.velocity_window
        if len(self._pos_prices) < n * 2:
            return None

        # Current velocity (last n samples)
        recent = self._pos_prices[-n:]
        dt1 = recent[-1][0] - recent[0][0]
        v1 = (recent[-1][1] - recent[0][1]) / dt1 if dt1 > 0 else 0.0

        # Previous velocity (n samples before that)
        older = self._pos_prices[-2 * n:-n]
        dt0 = older[-1][0] - older[0][0]
        v0 = (older[-1][1] - older[0][1]) / dt0 if dt0 > 0 else 0.0

        # Time between midpoints of both windows
        t_mid_1 = (recent[-1][0] + recent[0][0]) / 2
        t_mid_0 = (older[-1][0] + older[0][0]) / 2
        dt_mid = t_mid_1 - t_mid_0
        if dt_mid <= 0:
            return None
        return (v1 - v0) / dt_mid

    def should_exit(
        self,
        side: Side,
        entry_price: float,
        current_bid: float,
        current_ask: float,
        hold_seconds: float,
        seconds_to_close: Optional[float] = None,
        position_size: float = 0.0,
        ob_imbalance: float = 1.0,
    ) -> tuple[bool, str, str]:
        """
        Momentum exhaustion exit engine.
        All exits are taker (market sells).

        Returns (should_exit, reason, exit_mode).
        """
        now = time.time()
        self._pos_prices.append((now, current_bid))

        unrealized = current_bid - entry_price
        spread = current_ask - current_bid
        mid = (current_ask + current_bid) / 2.0
        spread_pct = (spread / mid) if mid > 0 else 1.0

        # Update trailing peak
        if current_bid > self._trailing_peak:
            self._trailing_peak = current_bid

        velocity = self.compute_velocity()
        acceleration = self.compute_acceleration()

        # ── 0. Monetary hard stop ────────────────────────────
        if position_size > 0 and self.hard_stop_loss_usd > 0:
            latent_pnl = unrealized * position_size
            if latent_pnl <= -self.hard_stop_loss_usd:
                logger.info(
                    "🚨 HARD STOP (USD): latent=%.2f€ ≤ -%.2f | "
                    "bid=%.4f entry=%.4f",
                    latent_pnl, self.hard_stop_loss_usd,
                    current_bid, entry_price,
                )
                return True, "hard_stop_usd", "taker"

        # ── 1. Percentage hard stop ──────────────────────────
        if unrealized <= -self.hard_stop_loss_pct:
            logger.info(
                "🚨 HARD STOP (PCT): Δ=%.4f ≤ -%.4f",
                unrealized, self.hard_stop_loss_pct,
            )
            return True, "hard_stop_pct", "taker"

        # ── 2. Time stop (close before market end) ───────────
        if seconds_to_close is not None:
            if seconds_to_close <= config.HARD_STOP_SECONDS:
                logger.info(
                    "🚨 HARD TIME STOP: %ds to close → force exit.",
                    int(seconds_to_close),
                )
                return True, "hard_time_stop", "taker"
            if seconds_to_close <= self.time_stop_before_close:
                logger.info(
                    "⏰ TIME STOP: %ds to close → exit (buffer=%ds).",
                    int(seconds_to_close), self.time_stop_before_close,
                )
                return True, "time_stop", "taker"

        # ── 3. Spread emergency (wide spread + losing) ───────
        if spread_pct > self.spread_emergency and unrealized <= 0:
            logger.info(
                "🚨 SPREAD EMERGENCY: %.1f%% > %.1f%% with Δ=%.4f",
                spread_pct * 100, self.spread_emergency * 100, unrealized,
            )
            return True, "spread_emergency", "taker"

        # ── 4. Trailing stop ─────────────────────────────────
        peak_gain = self._trailing_peak - entry_price
        if peak_gain >= self.trailing_activation:
            drawdown = self._trailing_peak - current_bid
            if drawdown >= self.trailing_distance:
                logger.info(
                    "📉 TRAILING STOP: peak=%.4f bid=%.4f "
                    "drawdown=%.4f ≥ %.4f (locked gain ~%.4f)",
                    self._trailing_peak, current_bid,
                    drawdown, self.trailing_distance,
                    current_bid - entry_price,
                )
                return True, "trailing_stop", "taker"

        # ── 5. OB collapse (protect gains) ───────────────────
        if ob_imbalance < self.ob_collapse_threshold and unrealized > 0.005:
            logger.info(
                "📊 OB COLLAPSE: imbalance=%.3f < %.3f → protecting gains",
                ob_imbalance, self.ob_collapse_threshold,
            )
            return True, "ob_collapse", "taker"

        # ── 6. Momentum exhaustion (velocity + acceleration) ─
        if velocity is not None and hold_seconds > 4:
            # Only engage kinematic exits when we have gains to protect
            if unrealized > 0.005:
                # 6a. Stall: velocity near zero for N consecutive ticks
                if abs(velocity) < self.stall_threshold:
                    self._stall_count += 1
                else:
                    self._stall_count = 0

                if self._stall_count >= self.stall_ticks:
                    logger.info(
                        "🐌 STALL EXIT: velocity=%.5f/s for %d ticks "
                        "(threshold=%.5f)",
                        velocity, self._stall_count, self.stall_threshold,
                    )
                    return True, "momentum_stall", "taker"

                # 6b. Negative acceleration + declining velocity
                if acceleration is not None:
                    if acceleration < self.accel_threshold and velocity < 0:
                        logger.info(
                            "📉 MOMENTUM EXHAUSTED: vel=%.5f/s accel=%.5f/s²",
                            velocity, acceleration,
                        )
                        return True, "momentum_exhaustion", "taker"

        # ── 7. Max hold time ─────────────────────────────────
        if hold_seconds >= self.max_hold_seconds:
            logger.info(
                "⏰ MAX HOLD: %ds ≥ %ds", int(hold_seconds), self.max_hold_seconds,
            )
            return True, "max_hold", "taker"

        return False, "", "taker"

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _get_yes_token(market: Market) -> Optional[Token]:
        for t in market.tokens:
            if t.outcome.lower() == "yes":
                return t
        return market.tokens[0] if market.tokens else None

    @staticmethod
    def _get_no_token(market: Market) -> Optional[Token]:
        for t in market.tokens:
            if t.outcome.lower() == "no":
                return t
        return market.tokens[1] if len(market.tokens) > 1 else None

