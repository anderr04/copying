"""
copy_engine.py – Motor de decisión para Copy-Trading híbrido.

Flujo por cada señal de whale:
    1. Enriquecer con datos del TokenRegistry (market_id, outcome, question).
    2. Consultar el orderbook CLOB actual (precio P_u, liquidez).
    3. Filtro de slippage: si P_u > P_b × (1 + MAX_SLIPPAGE), abortar.
    4. Si es BUY → copiar con el mismo token y dirección.
    5. Si es SELL y tenemos posición copiada → liquidar inmediatamente.
    6. Registrar todo para analytics.

Modos de ejecución:
    - "paper"  → usa el PaperTrader existente (sin tocar red).
    - "live"   → usa el LiveExecutor stub (py-clob-client, a conectar).

Restricción estricta: NO se modifica la inicialización de py-clob-client,
el manejo de claves privadas, las firmas ni el envío de órdenes existente.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import config
from src.event_decoder import WhaleTradeSignal
from src.token_registry import TokenRegistry
from src.accumulation import AccumulationTracker, PendingAccumulation
from src.polymarket_api import PolymarketClient, OrderbookSnapshot
from src.paper_trader import PaperTrader
from src.strategy import Side
from src.fees import calculate_dynamic_fee

logger = logging.getLogger(__name__)

WHALE_POSITIONS_FILE = config.DATA_DIR / "whale_positions.json"


# ── Whale Position Tracker ───────────────────────────────────────────

@dataclass
class WhalePosition:
    """Posición rastreada de una ballena en un token específico."""
    whale_label: str
    whale_address: str
    token_id: str
    outcome: str                # "Yes" / "No"
    market_question: str
    action: str                 # "BUY" siempre para posiciones abiertas
    entry_price: float
    size_tokens: float
    size_usd: float
    entry_time: float = field(default_factory=time.time)
    tx_hash: str = ""
    # Nuestra posición copiada
    our_token_id: str = ""      # token_id que compramos
    our_entry_price: float = 0.0
    our_size: float = 0.0
    copied: bool = False        # True si ejecutamos la copia


@dataclass
class CopyTradeResult:
    """Resultado de una decisión de copy-trade."""
    action: str                 # "COPIED", "LIQUIDATED", "SKIPPED_SLIPPAGE",
                                # "SKIPPED_SIZE", "SKIPPED_PRICE", "SKIPPED_UNKNOWN",
                                # "SKIPPED_NO_POSITION", "ACCUMULATED"
    whale_label: str
    token_id: str
    whale_price: float
    our_price: float = 0.0
    slippage_pct: float = 0.0
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    # Accumulation info
    accum_usd: float = 0.0      # total acumulado del whale en este token
    accum_fills: int = 0        # número de fills acumulados
    conviction_pct: float = 0.0 # convicción sobre el acumulado


# ── Live Executor Stub ───────────────────────────────────────────────

class LiveExecutor:
    """
    Stub de ejecución real vía py-clob-client.

    El usuario debe inicializar esta clase con su ClobClient existente.
    ESTA CLASE NO MANEJA claves privadas, firmas ni inicialización del client.
    Solo envuelve las llamadas de envío de órdenes.

    Para conectar:
        1. Importar tu ClobClient inicializado.
        2. Pasar la instancia al constructor.
        3. Implementar buy() y sell() con tus métodos existentes.
    """

    def __init__(self, clob_client=None):
        """
        Parámetros
        ----------
        clob_client : object (opcional)
            Instancia de py-clob-client ya inicializada.
            Si es None, todos los métodos loguean y retornan False.
        """
        self.client = clob_client
        self._connected = clob_client is not None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def buy(
        self,
        token_id: str,
        price: float,
        size: float,
    ) -> bool:
        """
        Coloca una orden de compra limit en el CLOB.

        TODO: Implementar con tu py-clob-client:
            order = self.client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=BUY,
                )
            )
        """
        if not self._connected:
            logger.warning(
                "LiveExecutor no conectado. BUY simulado: "
                "token=%s price=%.4f size=%.2f",
                token_id[:16] + "…", price, size,
            )
            return False

        # ── Placeholder: reemplazar con tu lógica real ──
        logger.info(
            "🔴 LIVE BUY │ token=%s │ price=%.4f │ size=%.2f",
            token_id[:16] + "…", price, size,
        )
        # order = self.client.create_and_post_order(...)
        # return order is not None
        return False

    def sell(
        self,
        token_id: str,
        price: float,
        size: float,
    ) -> bool:
        """
        Coloca una orden de venta limit en el CLOB.

        TODO: Implementar con tu py-clob-client:
            order = self.client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=SELL,
                )
            )
        """
        if not self._connected:
            logger.warning(
                "LiveExecutor no conectado. SELL simulado: "
                "token=%s price=%.4f size=%.2f",
                token_id[:16] + "…", price, size,
            )
            return False

        logger.info(
            "🔴 LIVE SELL │ token=%s │ price=%.4f │ size=%.2f",
            token_id[:16] + "…", price, size,
        )
        # order = self.client.create_and_post_order(...)
        # return order is not None
        return False


# ── Copy Trading Engine ──────────────────────────────────────────────

class CopyTradeEngine:
    """
    Motor principal de copy-trading con acumulación + sizing proporcional.

    Flujo:
        1. Cada OrderFilled → process_signal() → AccumulationTracker
        2. Periódicamente → check_accumulations() evalúa batches listos
        3. Batch listo + convicción suficiente → orderbook + slippage → COPY

    Las whales acumulan posiciones con múltiples fills pequeños.
    Ej: DrPufferfish compra $900 de Suns en 8 fills de $2-$492.
    Evaluamos la POSICIÓN TOTAL, no cada fill individual.

    Modelo de sizing (sobre el acumulado):
        conviction = whale_accum_usd / whale_portfolio_usd
        our_pct    = conviction × multiplier  (cap max_position_pct)
        our_size   = our_pct × our_capital    (floor min_trade_usd)
    """

    def __init__(
        self,
        clob_client: PolymarketClient,
        token_registry: TokenRegistry,
        whale_traders: dict[str, PaperTrader],
        live_executor: Optional[LiveExecutor] = None,
        mode: str = config.COPY_MODE,
        max_slippage: float = config.COPY_MAX_SLIPPAGE_PCT,
        min_whale_size_usd: float = config.COPY_MIN_WHALE_SIZE_USD,
        max_price: float = config.COPY_MAX_PRICE,
        min_price: float = config.COPY_MIN_PRICE,
        position_size_pct: float = config.COPY_POSITION_SIZE_PCT,
        # ── Proporcional sizing ─────────────────────────────
        whale_portfolios: dict[str, float] | None = None,
        default_whale_portfolio: float = config.DEFAULT_WHALE_PORTFOLIO_USD,
        min_conviction_pct: float = config.COPY_MIN_CONVICTION_PCT,
        conviction_multiplier: float = config.COPY_CONVICTION_MULTIPLIER,
        max_position_pct: float = config.COPY_MAX_POSITION_PCT,
        min_trade_usd: float = config.COPY_MIN_TRADE_USD,
    ):
        self.clob = clob_client
        self.registry = token_registry
        self.whale_traders = whale_traders
        self.live = live_executor or LiveExecutor()
        self.mode = mode

        # Filtros
        self.max_slippage = max_slippage
        self.min_whale_size_usd = min_whale_size_usd
        self.max_price = max_price
        self.min_price = min_price
        self.position_size_pct = position_size_pct

        # Proporcional sizing
        self.whale_portfolios = whale_portfolios or getattr(
            config, "WHALE_PORTFOLIOS", {}
        )
        self.default_whale_portfolio = default_whale_portfolio
        self.min_conviction_pct = min_conviction_pct
        self.conviction_multiplier = conviction_multiplier
        self.max_position_pct = max_position_pct
        self.min_trade_usd = min_trade_usd

        # ── Accumulation Tracker ─────────────────────────────
        self.accumulator = AccumulationTracker(
            whale_portfolios=self.whale_portfolios,
            default_whale_portfolio=self.default_whale_portfolio,
            min_conviction_pct=self.min_conviction_pct,
        )

        # Tracking
        self._whale_positions = {}
        self._results = []
        self._signals_processed = 0
        self._copies_executed = 0
        self._liquidations_executed = 0

    # ── Persistence ──────────────────────────────────────────────
    def save_whale_positions(self) -> None:
        """Save whale positions to JSON for restart recovery."""
        try:
            data = {}
            for key, wp in self._whale_positions.items():
                data[key] = {
                    "whale_label": wp.whale_label,
                    "whale_address": wp.whale_address,
                    "token_id": wp.token_id,
                    "outcome": wp.outcome,
                    "market_question": wp.market_question,
                    "action": wp.action,
                    "entry_price": wp.entry_price,
                    "size_tokens": wp.size_tokens,
                    "size_usd": wp.size_usd,
                    "entry_time": wp.entry_time,
                    "tx_hash": wp.tx_hash,
                    "our_token_id": wp.our_token_id,
                    "our_entry_price": wp.our_entry_price,
                    "our_size": wp.our_size,
                    "copied": wp.copied,
                }
            with open(WHALE_POSITIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.error("Error saving whale positions: %s", exc)

    def load_whale_positions(self) -> int:
        """Load whale positions from JSON. Returns count of positions loaded."""
        if not WHALE_POSITIONS_FILE.exists():
            return 0
        try:
            with open(WHALE_POSITIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in data:
                wpd = data[key]
                self._whale_positions[key] = WhalePosition(
                    whale_label=wpd["whale_label"],
                    whale_address=wpd["whale_address"],
                    token_id=wpd["token_id"],
                    outcome=wpd.get("outcome", ""),
                    market_question=wpd.get("market_question", ""),
                    action=wpd.get("action", "BUY"),
                    entry_price=wpd["entry_price"],
                    size_tokens=wpd.get("size_tokens", 0),
                    size_usd=wpd.get("size_usd", 0),
                    entry_time=wpd.get("entry_time", time.time()),
                    tx_hash=wpd.get("tx_hash", ""),
                    our_token_id=wpd.get("our_token_id", ""),
                    our_entry_price=wpd.get("our_entry_price", 0),
                    our_size=wpd.get("our_size", 0),
                    copied=wpd.get("copied", True),
                )
            logger.info("✅ Loaded %d whale positions from disk.", len(data))
            return len(data)
        except Exception as exc:
            logger.error("Error loading whale positions: %s", exc)
            return 0

    # ── Stats ────────────────────────────────────────────────────

    @property
    def signals_processed(self) -> int:
        return self._signals_processed

    @property
    def copies_executed(self) -> int:
        return self._copies_executed

    @property
    def liquidations_executed(self) -> int:
        return self._liquidations_executed

    # ── Main Entry Point ─────────────────────────────────────────

    def process_signal(self, signal: WhaleTradeSignal) -> CopyTradeResult:
        """
        Procesa una señal de trade de ballena.
        Alimenta el AccumulationTracker y retorna estado ACCUMULATED.
        La decisión real (COPY/SKIP) ocurre en check_accumulations()
        cuando el batch window expira o se cruza el instant threshold.
        """
        self._signals_processed += 1

        # 1. Enriquecer con token registry
        signal = self._enrich_signal(signal)

        # 2. Alimentar el accumulator (BUY y SELL van al mismo tracker)
        accum = self.accumulator.add_fill(
            whale_label=signal.whale_label,
            whale_address=signal.whale_address,
            token_id=signal.token_id,
            action=signal.action,
            size_usd=signal.size_usd,
            size_tokens=signal.size_tokens,
            price=signal.price,
            tx_hash=signal.tx_hash,
            outcome=signal.outcome,
            market_question=signal.market_question,
            condition_id=signal.condition_id,
        )

        # 3. Calcular convicción sobre el acumulado
        portfolio = self._get_whale_portfolio(signal.whale_label)
        conv_pct = (abs(accum.total_usd) / portfolio * 100) if portfolio > 0 else 0

        result = CopyTradeResult(
            action="ACCUMULATED",
            whale_label=signal.whale_label,
            token_id=signal.token_id,
            whale_price=signal.price,
            accum_usd=abs(accum.total_usd),
            accum_fills=accum.fill_count,
            conviction_pct=conv_pct,
        )

        self._results.append(result)
        return result

    # ── Conviction & Sizing ──────────────────────────────────────

    def _get_whale_portfolio(self, whale_label: str) -> float:
        """Retorna el portfolio estimado de un whale (USD)."""
        return self.whale_portfolios.get(
            whale_label, self.default_whale_portfolio
        )

    def _compute_conviction(self, whale_label: str, trade_usd: float) -> float:
        """Calcula la convicción: qué % de su portfolio arriesga el whale."""
        portfolio = self._get_whale_portfolio(whale_label)
        if portfolio <= 0:
            return 0.0
        return trade_usd / portfolio

    def _compute_our_size(
        self, conviction: float, available_capital: float,
    ) -> float:
        """
        Calcula nuestro tamaño de posición proporcional.

        our_pct  = conviction × multiplier   (capped at max_position_pct)
        our_size = our_pct × available_capital (floored at min_trade_usd)
        """
        our_pct = conviction * self.conviction_multiplier
        our_pct = min(our_pct, self.max_position_pct)
        our_size = our_pct * available_capital
        return our_size

    @staticmethod
    def _dynamic_max_slippage(
        whale_price: float,
        base_max: float = 0.05,
        margin_fraction: float = 0.30,
    ) -> float:
        """
        Slippage dinámico según el precio del whale.

        Lógica: a precios altos (0.90-0.99) queda poco margen hasta $1.00
        payout, así que el slippage tolerado se reduce automáticamente.

        max_slip = margin_fraction × (1.0 - P_b) / P_b
        Capped en base_max (para precios bajos donde el margen es enorme).

        Ejemplos (margin_fraction=0.30):
          P_b=0.50 → 30.0% → capped 5.0%
          P_b=0.80 →  7.5% → capped 5.0%
          P_b=0.90 →  3.3%
          P_b=0.93 →  2.3%
          P_b=0.95 →  1.6%
          P_b=0.98 →  0.6%
          P_b=0.99 →  0.3%
        """
        if whale_price <= 0 or whale_price >= 1.0:
            return 0.0  # Edge case: no trade
        margin = (1.0 - whale_price) / whale_price
        dynamic = margin_fraction * margin
        return min(dynamic, base_max)

    # ── Enriquecimiento ──────────────────────────────────────────

    def _enrich_signal(self, signal: WhaleTradeSignal) -> WhaleTradeSignal:
        """Complementa la señal con datos del market registry."""
        self.registry.refresh_if_stale()
        info = self.registry.lookup(signal.token_id)
        if info:
            signal.condition_id = info.condition_id
            signal.market_question = info.question
            signal.outcome = info.outcome
        else:
            logger.warning(
                "Token %s… no encontrado en registry. "
                "Market info no disponible.",
                signal.token_id[:20],
            )
        return signal

    # ── Handle Accumulated BUY ───────────────────────────────────

    def _handle_accumulated_buy(
        self, accum: PendingAccumulation,
    ) -> CopyTradeResult:
        """
        El whale ha acumulado compras → evaluar si copiamos.

        Usa el TOTAL ACUMULADO (no un fill individual) para:
            1. Calcular convicción.
            2. Calcular nuestro tamaño proporcional.
            3. Consultar orderbook actual.
            4. Slippage check.
            5. Ejecutar.
        """
        total_usd = abs(accum.total_usd)
        whale_portfolio = self._get_whale_portfolio(accum.whale_label)
        conviction = total_usd / whale_portfolio if whale_portfolio > 0 else 0

        # ── Calcular nuestro tamaño proporcional ──────────────
        trader = self.whale_traders.get(accum.whale_label)
        if self.mode == "paper" and trader:
            available = trader.available_capital
        else:
            available = 50.0
        our_size_usd = self._compute_our_size(conviction, available)

        if our_size_usd < self.min_trade_usd:
            reason = (
                f"Tamaño calculado ${our_size_usd:.2f} < mín ${self.min_trade_usd:.2f} │ "
                f"Acum ${total_usd:.2f} ({accum.fill_count} fills) │ "
                f"Conv {conviction:.4%}"
            )
            logger.debug("⏭️ %s: %s", accum.whale_label, reason)
            return CopyTradeResult(
                action="SKIPPED_SIZE",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=accum.avg_price,
                reason=reason,
                accum_usd=total_usd,
                accum_fills=accum.fill_count,
                conviction_pct=conviction * 100,
            )

        # ── Filtro: rango de precio ───────────────────────────
        P_b = accum.avg_price  # precio promedio del whale
        if P_b > self.max_price or P_b < self.min_price:
            reason = (
                f"Precio avg whale P_b={P_b:.4f} fuera de rango "
                f"[{self.min_price}, {self.max_price}]"
            )
            return CopyTradeResult(
                action="SKIPPED_PRICE",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=P_b,
                reason=reason,
                accum_usd=total_usd,
                accum_fills=accum.fill_count,
                conviction_pct=conviction * 100,
            )

        # ── Consultar orderbook CLOB ─────────────────────────
        snap = self._get_orderbook(accum.token_id)
        if snap is None:
            return CopyTradeResult(
                action="SKIPPED_UNKNOWN",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=P_b,
                reason="No se pudo obtener orderbook.",
                accum_usd=total_usd,
                accum_fills=accum.fill_count,
                conviction_pct=conviction * 100,
            )

        P_u = snap.best_ask

        # ── HARD CEILING: nunca pagar ≥ $0.99 ────────────────
        if P_u >= 0.99:
            reason = (
                f"HARD CEILING: P_u={P_u:.4f} ≥ 0.99 "
                f"(margen < 1¢, no vale la pena)"
            )
            logger.warning(
                "🚫 %s │ BUY │ P_u=%.4f ≥ 0.99 │ ABORTADO (sin margen)",
                accum.whale_label, P_u,
            )
            return CopyTradeResult(
                action="SKIPPED_SLIPPAGE",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=P_b,
                our_price=P_u,
                slippage_pct=(P_u - P_b) / P_b if P_b > 0 else 0,
                reason=reason,
                accum_usd=total_usd,
                accum_fills=accum.fill_count,
                conviction_pct=conviction * 100,
            )

        # ── DYNAMIC SLIPPAGE CHECK ───────────────────────────
        dyn_max_slip = self._dynamic_max_slippage(P_b, base_max=self.max_slippage)
        slippage_threshold = P_b * (1.0 + dyn_max_slip)
        slippage_pct = (P_u - P_b) / P_b if P_b > 0 else float("inf")

        if P_u > slippage_threshold:
            reason = (
                f"SLIPPAGE: P_u={P_u:.4f} > "
                f"P_b×{1+dyn_max_slip:.3f}={slippage_threshold:.4f} "
                f"(dyn_max={dyn_max_slip:.2%} para P_b={P_b:.2f})"
            )
            logger.warning(
                "🚫 %s │ BUY │ Acum $%.2f (%d fills) │ P_b=%.4f → P_u=%.4f "
                "(slip=%+.2f%% > dyn_max=%.2f%%) │ ABORTADO",
                accum.whale_label, total_usd, accum.fill_count,
                P_b, P_u, slippage_pct * 100, dyn_max_slip * 100,
            )
            return CopyTradeResult(
                action="SKIPPED_SLIPPAGE",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=P_b,
                our_price=P_u,
                slippage_pct=slippage_pct,
                reason=reason,
                accum_usd=total_usd,
                accum_fills=accum.fill_count,
                conviction_pct=conviction * 100,
            )

        # ── EJECUTAR COPIA ───────────────────────────────────
        our_pct = min(conviction * self.conviction_multiplier, self.max_position_pct)

        logger.info(
            "✅ COPY BUY │ %s │ %s [%s] │ "
            "Acum $%.2f (%d fills, %.0fs) │ Conv %.4f%% │ "
            "P_avg=%.4f → P_u=%.4f (slip=%+.2f%%) │ "
            "Nosotros $%.2f (%.1f%% capital)",
            accum.whale_label,
            accum.outcome or "?",
            accum.market_question[:40] or accum.token_id[:16] + "…",
            total_usd, accum.fill_count, accum.age_s,
            conviction * 100,
            P_b, P_u, slippage_pct * 100,
            our_size_usd, our_pct * 100,
        )

        # Build a synthetic signal for the executor
        synth_signal = WhaleTradeSignal(
            tx_hash=accum.last_tx_hash,
            block_number=0,
            whale_address=accum.whale_address,
            whale_label=accum.whale_label,
            whale_role="accumulated",
            token_id=accum.token_id,
            action="BUY",
            price=P_b,
            size_tokens=accum.total_tokens,
            size_usd=total_usd,
            fee_usd=0.0,
            condition_id=accum.condition_id,
            outcome=accum.outcome,
            market_question=accum.market_question,
        )

        success = self._execute_buy(synth_signal, snap, size_usd=our_size_usd)

        if success:
            pos_key = f"{accum.whale_address}:{accum.token_id}"
            self._whale_positions[pos_key] = WhalePosition(
                whale_label=accum.whale_label,
                whale_address=accum.whale_address,
                token_id=accum.token_id,
                outcome=accum.outcome,
                market_question=accum.market_question,
                action="BUY",
                entry_price=P_b,
                size_tokens=accum.total_tokens,
                size_usd=total_usd,
                tx_hash=accum.last_tx_hash,
                our_token_id=accum.token_id,
                our_entry_price=P_u,
                our_size=our_size_usd / P_u if P_u > 0 else 0,
                copied=True,
            )
            self._copies_executed += 1
            self.save_whale_positions()

            return CopyTradeResult(
                action="COPIED",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=P_b,
                our_price=P_u,
                slippage_pct=slippage_pct,
                accum_usd=total_usd,
                accum_fills=accum.fill_count,
                conviction_pct=conviction * 100,
            )
        else:
            return CopyTradeResult(
                action="SKIPPED_UNKNOWN",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=P_b,
                our_price=P_u,
                reason="Error en ejecución de orden.",
                accum_usd=total_usd,
                accum_fills=accum.fill_count,
                conviction_pct=conviction * 100,
            )

    # ── Handle Accumulated SELL ──────────────────────────────────

    def _handle_accumulated_sell(
        self, accum: PendingAccumulation,
    ) -> CopyTradeResult:
        """
        El whale ha acumulado ventas → liquidar si tenemos posición.
        """
        pos_key = f"{accum.whale_address}:{accum.token_id}"
        whale_pos = self._whale_positions.get(pos_key)

        if whale_pos is None or not whale_pos.copied:
            return CopyTradeResult(
                action="SKIPPED_NO_POSITION",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=accum.avg_price,
                reason="Sin posición copiada para este token.",
            )

        snap = self._get_orderbook(accum.token_id)
        P_u = snap.best_bid if snap else accum.avg_price

        logger.info(
            "🔻 LIQUIDACIÓN │ %s VENDE │ %s [%s] │ "
            "Acum $%.2f (%d fills) │ bid=%.4f",
            accum.whale_label,
            whale_pos.outcome or "?",
            whale_pos.market_question[:40] or accum.token_id[:16] + "…",
            abs(accum.total_usd), accum.fill_count, P_u,
        )

        synth_signal = WhaleTradeSignal(
            tx_hash=accum.last_tx_hash,
            block_number=0,
            whale_address=accum.whale_address,
            whale_label=accum.whale_label,
            whale_role="accumulated",
            token_id=accum.token_id,
            action="SELL",
            price=accum.avg_price,
            size_tokens=abs(accum.total_tokens),
            size_usd=abs(accum.total_usd),
            fee_usd=0.0,
        )

        success = self._execute_sell(synth_signal, snap)

        if success:
            del self._whale_positions[pos_key]
            self._liquidations_executed += 1
            self.save_whale_positions()
            return CopyTradeResult(
                action="LIQUIDATED",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=accum.avg_price,
                our_price=P_u,
            )
        else:
            return CopyTradeResult(
                action="SKIPPED_UNKNOWN",
                whale_label=accum.whale_label,
                token_id=accum.token_id,
                whale_price=accum.avg_price,
                our_price=P_u,
                reason="Error al liquidar.",
            )

    # ── Ejecución ────────────────────────────────────────────────

    def _execute_buy(
        self, signal: WhaleTradeSignal, snap: OrderbookSnapshot,
        *, size_usd: float = 0.0,
    ) -> bool:
        """Ejecuta una compra (paper o live) con tamaño proporcional."""
        if self.mode == "paper":
            return self._paper_buy(signal, snap, size_usd=size_usd)
        else:
            return self._live_buy(signal, snap, size_usd=size_usd)

    def _execute_sell(
        self, signal: WhaleTradeSignal, snap: Optional[OrderbookSnapshot],
    ) -> bool:
        """Ejecuta una venta/liquidación (paper o live)."""
        if self.mode == "paper":
            return self._paper_sell(signal, snap)
        else:
            return self._live_sell(signal, snap)

    def _paper_buy(
        self, signal: WhaleTradeSignal, snap: OrderbookSnapshot,
        *, size_usd: float = 0.0,
    ) -> bool:
        """Compra via PaperTrader con tamaño proporcional por whale."""
        side = Side.YES if signal.outcome.lower() == "yes" else Side.NO
        whale_label = signal.whale_label
        trader = self.whale_traders.get(whale_label)
        if trader is None:
            logger.warning(f"No PaperTrader para whale {whale_label}")
            return False
        if size_usd > 0 and trader.available_capital > 0:
            override_pct = size_usd / trader.available_capital
            override_pct = min(override_pct, self.max_position_pct)
            old_pct = config.POSITION_SIZE_PCT
            config.POSITION_SIZE_PCT = override_pct
        else:
            old_pct = None

        pos = trader.open_trade(
            market_id=signal.condition_id or signal.token_id,
            market_question=signal.market_question or f"Copy {signal.whale_label}",
            token_id=signal.token_id,
            side=side,
            entry_price=snap.best_ask,
            best_bid=snap.best_bid,
            spread=snap.spread,
            ob_imbalance=snap.imbalance(config.IMBALANCE_LEVELS),
            yes_liquidity=snap.bid_depth(config.IMBALANCE_LEVELS),
            no_liquidity=snap.ask_depth(config.IMBALANCE_LEVELS),
            entry_spread_pct=snap.spread_pct,
            orderbook_asks=snap.asks if config.USE_DYNAMIC_SLIPPAGE else None,
        )

        if old_pct is not None:
            config.POSITION_SIZE_PCT = old_pct

        return pos is not None

    def _paper_sell(
        self, signal: WhaleTradeSignal, snap: Optional[OrderbookSnapshot],
    ) -> bool:
        """Venta / liquidación via PaperTrader por whale."""
        whale_label = signal.whale_label
        trader = self.whale_traders.get(whale_label)
        token_id = signal.token_id
        if trader is None:
            logger.warning(f"No PaperTrader para whale {whale_label}")
            return False
        if token_id not in trader.open_positions:
            logger.warning("PaperTrader sin posición abierta para token %s…", token_id[:16])
            return False

        exit_price = snap.best_bid if snap else signal.price
        closed = trader.close_trade(
            exit_price=exit_price,
            reason=f"Whale {signal.whale_label} liquidó posición",
            is_maker=False,
            token_id=token_id,
        )
        return closed is not None

    def _live_buy(
        self, signal: WhaleTradeSignal, snap: OrderbookSnapshot,
        *, size_usd: float = 0.0,
    ) -> bool:
        """Compra via LiveExecutor (py-clob-client stub)."""
        if size_usd > 0:
            alloc = size_usd
        else:
            trader = self.whale_traders.get(signal.whale_label)
            cap = trader.available_capital if trader else 50.0
            alloc = cap * self.position_size_pct
        size = alloc / snap.best_ask if snap.best_ask > 0 else 0
        return self.live.buy(
            token_id=signal.token_id,
            price=snap.best_ask,
            size=size,
        )

    def _live_sell(
        self, signal: WhaleTradeSignal, snap: Optional[OrderbookSnapshot],
    ) -> bool:
        """Venta via LiveExecutor (py-clob-client stub)."""
        pos_key = f"{signal.whale_address}:{signal.token_id}"
        whale_pos = self._whale_positions.get(pos_key)
        size = whale_pos.our_size if whale_pos else 0
        price = snap.best_bid if snap else signal.price
        return self.live.sell(
            token_id=signal.token_id,
            price=price,
            size=size,
        )

    # ── Orderbook helper ─────────────────────────────────────────

    def _get_orderbook(self, token_id: str) -> Optional[OrderbookSnapshot]:
        """
        Consulta el orderbook CLOB para un token.
        Retorna None si no se puede obtener.
        """
        try:
            snap = self.clob.get_orderbook(token_id)
            if snap.best_ask <= 0 or snap.best_bid <= 0:
                logger.warning(
                    "Orderbook vacío/inválido para token %s…",
                    token_id[:16],
                )
                return None
            return snap
        except Exception as exc:
            logger.error("Error obteniendo orderbook: %s", exc)
            return None

    # ── Position Management ──────────────────────────────────────

    def check_positions(self) -> list[CopyTradeResult]:
        """
        Chequeo periódico: evalúa acumulaciones listas + posiciones.
        Se llama desde el main loop cada ~1s.
        """
        return self.check_accumulations()

    def check_accumulations(self) -> list[CopyTradeResult]:
        """
        Evalúa batches de acumulación listos (window expirado o instant trigger).
        Para cada batch listo, decide si copiar (BUY) o liquidar (SELL).
        """
        results: list[CopyTradeResult] = []

        # Limpiar acumulaciones stale
        self.accumulator.cleanup_stale()

        # Obtener batches listos
        ready = self.accumulator.check_ready()
        for accum in ready:
            side = accum.net_side
            if side == "BUY":
                result = self._handle_accumulated_buy(accum)
            elif side == "SELL":
                result = self._handle_accumulated_sell(accum)
            else:
                continue

            # Marcar como done SIEMPRE tras evaluar (evita re-evaluación infinita).
            # Si fue COPIED/LIQUIDATED → posición abierta.
            # Si fue SKIPPED_* → oportunidad descartada, no reintentar.
            self.accumulator.mark_copied(
                accum.whale_address, accum.token_id,
            )

            # Guardar resultado para el summary
            self._results.append(result)
            results.append(result)

        return results

    def get_tracked_positions(self) -> list[WhalePosition]:
        """Retorna las posiciones de ballenas que estamos copiando."""
        return [p for p in self._whale_positions.values() if p.copied]

    # ── Summary ──────────────────────────────────────────────────

    def print_summary(self) -> None:
        """Imprime resumen del engine + acumulaciones."""
        print("\n" + "=" * 62)
        print("  📊  COPY-TRADE ENGINE SUMMARY")
        print("=" * 62)
        print(f"  Señales procesadas  : {self._signals_processed}")
        print(f"  Copias ejecutadas   : {self._copies_executed}")
        print(f"  Liquidaciones       : {self._liquidations_executed}")
        print(f"  Posiciones activas  : {len(self.get_tracked_positions())}")

        # Breakdown de resultados
        action_counts: dict[str, int] = {}
        for r in self._results:
            action_counts[r.action] = action_counts.get(r.action, 0) + 1
        if action_counts:
            print(f"  ── Breakdown ──")
            for action, count in sorted(action_counts.items()):
                print(f"     {action:25s}: {count}")

        # Posiciones activas
        tracked = self.get_tracked_positions()
        if tracked:
            print(f"  ── Posiciones copiadas activas ──")
            for pos in tracked:
                print(
                    f"     🐋 {pos.whale_label:15s} │ {pos.outcome:3s} │ "
                    f"Whale P={pos.entry_price:.4f} │ Ours P={pos.our_entry_price:.4f} │ "
                    f"${pos.size_usd:.2f}"
                )

        print("=" * 62 + "\n")

        # Accumulation summary
        self.accumulator.print_summary()
