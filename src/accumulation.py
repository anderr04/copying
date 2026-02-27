"""
accumulation.py – Rastreo de posiciones acumuladas de whales.

Polymarket ejecuta órdenes grandes como MÚLTIPLES OrderFilled events
(matcheando contra diferentes órdenes limit en el book).  Un whale
que "apuesta $900 en Suns" genera 8+ eventos de $2-$492 cada uno.

Este módulo agrupa esos fills individuales por (whale, token_id) y
evalúa la convicción sobre el TOTAL ACUMULADO, no sobre cada fill.

Dos modos de trigger:
    1. BATCH: espera BATCH_WINDOW_S sin nuevos fills → evalúa el batch.
    2. THRESHOLD: evalúa inmediatamente si el acumulado cruza el umbral
       de convicción (para fills grandes que no necesitan espera).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class PendingAccumulation:
    """Posición pendiente acumulándose de un whale en un token."""
    whale_label: str
    whale_address: str
    token_id: str
    outcome: str
    market_question: str
    condition_id: str

    # Acumulación
    total_usd: float = 0.0
    total_tokens: float = 0.0
    fill_count: int = 0
    first_fill_time: float = field(default_factory=time.time)
    last_fill_time: float = field(default_factory=time.time)
    avg_price: float = 0.0

    # Estado
    copied: bool = False           # Ya emitimos señal de copy
    copy_at_usd: float = 0.0      # USD acumulado cuando copiamos

    # Último signal (para referenciar en el copy)
    last_action: str = "BUY"
    last_tx_hash: str = ""

    def add_fill(
        self, size_usd: float, size_tokens: float, price: float,
        action: str, tx_hash: str,
    ) -> None:
        """Añade un fill a la acumulación."""
        self.fill_count += 1
        self.last_fill_time = time.time()
        self.last_action = action
        self.last_tx_hash = tx_hash

        if action == "BUY":
            self.total_usd += size_usd
            self.total_tokens += size_tokens
        elif action == "SELL":
            self.total_usd -= size_usd
            self.total_tokens -= size_tokens

        # Recalcular precio promedio
        if self.total_tokens > 0:
            self.avg_price = self.total_usd / self.total_tokens
        else:
            self.avg_price = price

    @property
    def age_s(self) -> float:
        """Segundos desde el primer fill."""
        return time.time() - self.first_fill_time

    @property
    def idle_s(self) -> float:
        """Segundos desde el último fill."""
        return time.time() - self.last_fill_time

    @property
    def net_side(self) -> str:
        """BUY si acumulación neta positiva, SELL si negativa."""
        return "BUY" if self.total_usd >= 0 else "SELL"


class AccumulationTracker:
    """
    Rastrea posiciones acumuladas de whales por (whale_address, token_id).

    Flujo:
        1. add_fill() → cada OrderFilled se agrega a la acumulación.
        2. check_ready() → retorna acumulaciones listas para evaluar:
           - batch_window expirado (sin fills nuevos por N segundos), O
           - threshold cruzado (acumulación > min conviction threshold).
        3. mark_copied() → marca que ya emitimos la señal de copy.
        4. check_idle() → limpia acumulaciones sin actividad.
    """

    def __init__(
        self,
        batch_window_s: float = 0.0,
        whale_portfolios: dict[str, float] | None = None,
        default_whale_portfolio: float = 0.0,
        min_conviction_pct: float = 0.0,
        stale_timeout_s: float = 0.0,
    ):
        # Cargar defaults de config si no se pasan
        self.batch_window_s = batch_window_s or getattr(
            config, "COPY_BATCH_WINDOW_S", 120.0
        )
        self.whale_portfolios = whale_portfolios or getattr(
            config, "WHALE_PORTFOLIOS", {}
        )
        self.default_whale_portfolio = default_whale_portfolio or getattr(
            config, "DEFAULT_WHALE_PORTFOLIO_USD", 1_000_000.0
        )
        self.min_conviction_pct = min_conviction_pct or getattr(
            config, "COPY_MIN_CONVICTION_PCT", 0.001
        )
        self.stale_timeout_s = stale_timeout_s or getattr(
            config, "COPY_ACCUM_STALE_S", 3600.0
        )

        # Estado: (whale_address, token_id) → PendingAccumulation
        self._pending: dict[str, PendingAccumulation] = {}

        # Estadísticas
        self.total_fills_received: int = 0
        self.total_batches_evaluated: int = 0
        self.total_batches_passed: int = 0
        self.total_batches_skipped: int = 0

    @staticmethod
    def _key(whale_address: str, token_id: str) -> str:
        return f"{whale_address.lower()}:{token_id}"

    def _get_whale_portfolio(self, whale_label: str) -> float:
        return self.whale_portfolios.get(
            whale_label, self.default_whale_portfolio
        )

    # ── Public API ───────────────────────────────────────────────

    def add_fill(
        self,
        whale_label: str,
        whale_address: str,
        token_id: str,
        action: str,         # "BUY" / "SELL"
        size_usd: float,
        size_tokens: float,
        price: float,
        tx_hash: str = "",
        outcome: str = "",
        market_question: str = "",
        condition_id: str = "",
    ) -> PendingAccumulation:
        """
        Registra un fill en la acumulación.
        Retorna la acumulación actual para logging.
        """
        self.total_fills_received += 1
        key = self._key(whale_address, token_id)

        if key not in self._pending:
            self._pending[key] = PendingAccumulation(
                whale_label=whale_label,
                whale_address=whale_address,
                token_id=token_id,
                outcome=outcome,
                market_question=market_question,
                condition_id=condition_id,
            )

        accum = self._pending[key]

        # Actualizar metadata si viene mejor datos
        if outcome and not accum.outcome:
            accum.outcome = outcome
        if market_question and not accum.market_question:
            accum.market_question = market_question
        if condition_id and not accum.condition_id:
            accum.condition_id = condition_id

        accum.add_fill(size_usd, size_tokens, price, action, tx_hash)

        # Log del fill individual
        portfolio = self._get_whale_portfolio(whale_label)
        conv_pct = abs(accum.total_usd) / portfolio * 100 if portfolio > 0 else 0
        logger.debug(
            "📊 FILL #%d │ %s │ %s %s │ $%.2f │ "
            "ACUM: $%.2f (%d fills, conv %.4f%%)",
            accum.fill_count, whale_label, action,
            outcome or "?", size_usd,
            accum.total_usd, accum.fill_count, conv_pct,
        )

        return accum

    def check_ready(self) -> list[PendingAccumulation]:
        """
        Retorna acumulaciones listas para evaluar:
          (a) batch_window expirado sin nuevos fills, O
          (b) convicción ya cruzó el threshold (instant trigger).

        Solo retorna acumulaciones NO copiadas aún.
        """
        ready: list[PendingAccumulation] = []
        now = time.time()

        for key, accum in list(self._pending.items()):
            if accum.copied:
                continue

            abs_usd = abs(accum.total_usd)
            if abs_usd < 0.01:
                continue  # nada significativo acumulado

            portfolio = self._get_whale_portfolio(accum.whale_label)
            conviction = abs_usd / portfolio if portfolio > 0 else 0

            # ── (a) Batch window expirado ──
            idle = now - accum.last_fill_time
            if idle >= self.batch_window_s:
                self.total_batches_evaluated += 1
                if conviction >= self.min_conviction_pct:
                    ready.append(accum)
                    self.total_batches_passed += 1
                    logger.info(
                        "🎯 BATCH READY │ %s │ %s [%s] │ "
                        "$%.2f (%d fills en %.0fs) │ conv %.4f%%",
                        accum.whale_label,
                        accum.outcome or "?",
                        accum.market_question[:40] or accum.token_id[:16],
                        abs_usd, accum.fill_count, accum.age_s,
                        conviction * 100,
                    )
                else:
                    self.total_batches_skipped += 1
                    logger.debug(
                        "⏭️ BATCH SKIP │ %s │ $%.2f │ conv %.4f%% < %.2f%%",
                        accum.whale_label, abs_usd,
                        conviction * 100, self.min_conviction_pct * 100,
                    )
                    # Marca como "evaluado" para no re-evaluar
                    accum.copied = True  # reutilizamos flag para marcar descarte
                continue

            # ── (b) Instant threshold: convicción alta sin esperar ──
            # Si la acumulación ya es muy grande, no esperar al batch
            # Umbral: 3x la convicción mínima (señal fuerte)
            instant_threshold = self.min_conviction_pct * 3
            if conviction >= instant_threshold:
                # Guard: marcar como evaluado ANTES de retornar
                # para evitar re-evaluación infinita si copy/slippage falla
                accum.copied = True
                ready.append(accum)
                self.total_batches_evaluated += 1
                self.total_batches_passed += 1
                logger.info(
                    "⚡ INSTANT TRIGGER │ %s │ %s [%s] │ "
                    "$%.2f (%d fills) │ conv %.4f%% ≥ %.2f%%",
                    accum.whale_label,
                    accum.outcome or "?",
                    accum.market_question[:40] or accum.token_id[:16],
                    abs_usd, accum.fill_count,
                    conviction * 100, instant_threshold * 100,
                )

        return ready

    def mark_copied(self, whale_address: str, token_id: str) -> None:
        """Marca una acumulación como ya copiada."""
        key = self._key(whale_address, token_id)
        accum = self._pending.get(key)
        if accum:
            accum.copied = True
            accum.copy_at_usd = accum.total_usd

    def get_accum(
        self, whale_address: str, token_id: str,
    ) -> Optional[PendingAccumulation]:
        """Retorna la acumulación actual (o None)."""
        key = self._key(whale_address, token_id)
        return self._pending.get(key)

    def cleanup_stale(self) -> int:
        """Elimina acumulaciones sin actividad por más de stale_timeout_s."""
        now = time.time()
        to_delete = [
            k for k, a in self._pending.items()
            if (now - a.last_fill_time) > self.stale_timeout_s
        ]
        for k in to_delete:
            del self._pending[k]
        if to_delete:
            logger.debug("🗑 Limpiados %d acumulaciones stale.", len(to_delete))
        return len(to_delete)

    @property
    def active_count(self) -> int:
        """Acumulaciones activas (no copiadas, con saldo)."""
        return sum(
            1 for a in self._pending.values()
            if not a.copied and abs(a.total_usd) > 0.01
        )

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def print_summary(self) -> None:
        """Imprime resumen de acumulaciones."""
        print("\n" + "=" * 68)
        print("  📊  ACCUMULATION TRACKER SUMMARY")
        print("=" * 68)
        print(f"  Fills recibidos     : {self.total_fills_received}")
        print(f"  Batches evaluados   : {self.total_batches_evaluated}")
        print(f"  Batches → COPY      : {self.total_batches_passed}")
        print(f"  Batches → SKIP      : {self.total_batches_skipped}")
        print(f"  Acumulaciones activas: {self.active_count}")

        # Top posiciones activas
        active = [
            a for a in self._pending.values()
            if not a.copied and abs(a.total_usd) > 0.01
        ]
        active.sort(key=lambda a: abs(a.total_usd), reverse=True)

        if active:
            print(f"  ── Posiciones acumulándose ──")
            for a in active[:10]:
                port = self._get_whale_portfolio(a.whale_label)
                conv = abs(a.total_usd) / port * 100 if port > 0 else 0
                print(
                    f"     {a.whale_label:15s} │ {a.net_side:4s} │ "
                    f"${abs(a.total_usd):>9.2f} ({a.fill_count} fills) │ "
                    f"conv {conv:.4f}% │ {a.outcome or '?':3s} │ "
                    f"{a.market_question[:35] or a.token_id[:16]}"
                )

        print("=" * 68 + "\n")
