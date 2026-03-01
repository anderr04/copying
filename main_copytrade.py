#!/usr/bin/env python3
"""
main_copytrade.py – Entry point del Bot de Copy-Trading Híbrido.

Arquitectura:
    ┌─────────────────┐     ┌──────────────┐
    │  Polygon WSS    │────▶│  Listener    │  (TOPIC-FILTERED: solo whale events)
    │  (Alchemy node) │     │  (Thread)    │  (ZERO RPC calls)
    └─────────────────┘     └──────┬───────┘
                                   │ queue.Queue (WhaleTradeSignal)
                                   ▼
    ┌─────────────────┐     ┌──────────────┐     ┌──────────────┐
    │  Token Registry  │◀──▶│  Copy Engine  │────▶│  Executor    │
    │  (Gamma API)     │    │  (Main loop)  │     │  Paper/STUB  │
    └─────────────────┘     └──────┬───────┘     └──────────────┘
                                   │
                                   ▼
                            ┌──────────────┐
                            │  CLOB API    │
                            │  (Orderbook) │
                            └──────────────┘

Arquitectura Zero-RPC:
    Suscripción FILTRADA por topics — Alchemy solo envía eventos
    donde maker (topic[2]) o taker (topic[3]) es una ballena conocida.
    CERO llamadas eth_getTransactionByHash. CERO HTTP polling.
    Consumo de CU: ~20 al suscribirse + 0 por evento recibido.

Log determinístico por cada match:
    [TIMESTAMP_LOCAL] MATCH WHALE: <Dirección> | MARKET_ID: <Hash> |
    SIDE: <BUY/SELL> | ASSET: <YES/NO> | PRECIO: <Pb calculado> |
    TAMAÑO: <Volumen USDC>

Run:
    python main_copytrade.py                   # paper mode (default)
    python main_copytrade.py --mode live       # live mode (stub – sin ejecución real)
    python main_copytrade.py --once            # procesa un signal y sale
"""

from __future__ import annotations

import argparse
import queue
import signal
import sys
import time
import logging
from datetime import datetime, timezone

import config
from src.logger import setup_logging, TradeLogger
from src.polymarket_api import PolymarketClient
from src.paper_trader import PaperTrader
from src.event_decoder import EventDecoder, WhaleTradeSignal
from src.wallet_filter import WalletFilter
from src.token_registry import TokenRegistry
from src.web3_listener import BlockchainListener
from src.copy_engine import CopyTradeEngine, LiveExecutor

logger = logging.getLogger("copytrade")

# Graceful shutdown flag
_shutdown = False


def _handle_sigint(signum, frame):
    global _shutdown
    logger.info("Interrupt recibido – deteniendo tras ciclo actual…")
    _shutdown = True


signal.signal(signal.SIGINT, _handle_sigint)


# ── Deterministic Log Format ─────────────────────────────────────────

_DECISION_FMT = (
    "[{ts}] DECISION: {action} | WHALE: {whale} | "
    "SIDE: {side} | ASSET: {asset} | "
    "P_whale: {pw:.6f} | P_ours: {pu:.6f} | SLIP: {slip:+.4f}% | "
    "WHALE_ACCUM: ${waccum:.2f} ({fills} fills) | CONV: {conv:.4f}% | "
    "OUR_SIZE: ${osize:.2f}"
)

_ACCUM_FMT = (
    "[{ts}] FILL #{fill_n} | WHALE: {whale} | "
    "MARKET: {question} | {side} ${size:.2f} | "
    "ACCUM: ${accum:.2f} ({fills} fills) | CONV: {conv:.4f}% | "
    "BATCH: {idle:.0f}/{window:.0f}s"
)

_HEARTBEAT_FMT = (
    "[{ts}] HEARTBEAT | events_recv={recv} | whale_match={match} | "
    "signals_proc={proc} | copies={copies} | positions={pos} | "
    "capital={capital:.2f} | mode=TOPIC_FILTERED"
)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Main ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket Whale Copy-Trading Bot – Hybrid (Web3 + CLOB)",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=config.COPY_MODE,
        help="Modo de ejecución: 'paper' (simulado) o 'live' (stub).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Procesa una señal y sale.",
    )
    args = parser.parse_args()
    mode = args.mode

    # ── Inicializar subsistemas ──────────────────────────────────
    setup_logging()

    # Validar configuración crítica
    if not config.WHALE_WALLETS:
        logger.error(
            "❌ No hay wallets de ballenas configuradas.\n"
            "   Configura WHALE_WALLETS en config.py o vía .env:\n"
            "   WHALE_WALLETS=DrPufferfish:0x...,LucasMeow:0x...\n"
        )
        sys.exit(1)

    if "YOUR_ALCHEMY_API_KEY" in config.POLYGON_WSS_URL:
        logger.error(
            "❌ Endpoint de Alchemy no configurado.\n"
            "   Configura POLYGON_WSS_URL en .env:\n"
            "   POLYGON_WSS_URL=wss://polygon-mainnet.g.alchemy.com/v2/TU_API_KEY\n"
        )
        sys.exit(1)

    # Componentes
    clob_client = PolymarketClient()
    # Per-whale PaperTrader instances (separate state files)
    whale_traders = {
        'DrPufferfish': PaperTrader(initial_capital=100.0, label='DrPufferfish'),
        'LucasMeow': PaperTrader(initial_capital=100.0, label='LucasMeow'),
        'Tsybka': PaperTrader(initial_capital=100.0, label='Tsybka'),
    }
    trade_logger = TradeLogger()
    # NO wipe CSV — preserve historical data across restarts

    decoder = EventDecoder()
    wallet_filter = WalletFilter(
        wallets=config.WHALE_WALLETS,
        proxies=config.WHALE_PROXIES,
    )
    token_registry = TokenRegistry()
    signal_queue: queue.Queue[WhaleTradeSignal] = queue.Queue(maxsize=1000)

    # Live executor → STUB only (no real py-clob-client connection)
    live_executor = LiveExecutor(clob_client=None)

    engine = CopyTradeEngine(
        clob_client=clob_client,
        token_registry=token_registry,
        whale_traders=whale_traders,
        live_executor=live_executor,
        mode=mode,
        max_slippage=config.COPY_MAX_SLIPPAGE_PCT,
        min_whale_size_usd=config.COPY_MIN_WHALE_SIZE_USD,
        max_price=config.COPY_MAX_PRICE,
        min_price=config.COPY_MIN_PRICE,
        position_size_pct=config.COPY_POSITION_SIZE_PCT,
        # Proporcional sizing
        whale_portfolios=config.WHALE_PORTFOLIOS,
        default_whale_portfolio=config.DEFAULT_WHALE_PORTFOLIO_USD,
        min_conviction_pct=config.COPY_MIN_CONVICTION_PCT,
        conviction_multiplier=config.COPY_CONVICTION_MULTIPLIER,
        max_position_pct=config.COPY_MAX_POSITION_PCT,
        min_trade_usd=config.COPY_MIN_TRADE_USD,
    )

    listener = BlockchainListener(
        decoder=decoder,
        wallet_filter=wallet_filter,
        signal_queue=signal_queue,
        token_registry=token_registry,
    )

    # ── Restore persisted state (survives restarts) ──────────────
    for trader in whale_traders.values():
        trader.load_state()
    n_whale_pos = engine.load_whale_positions()
    total_positions = sum(len(trader.open_positions) for trader in whale_traders.values())
    total_capital = sum(trader.available_capital for trader in whale_traders.values())
    if n_whale_pos > 0 or total_positions:
        logger.info(
            "📦 State restored: %d whale positions, %d paper positions, $%.2f capital",
            n_whale_pos, total_positions, total_capital,
        )
        for label, trader in whale_traders.items():
            logger.info(f"  🐋 {label}: {len(trader.open_positions)} posiciones, capital ${trader.available_capital:.2f}")

    # ── Restart marker in CSV ────────────────────────────────────
    trade_logger.log_restart_marker(
        n_open_positions=total_positions,
        capital=total_capital,
    )
    for label, trader in whale_traders.items():
        logger.info(f"  🐋 {label}: {len(trader.open_positions)} posiciones, capital ${trader.available_capital:.2f}")

    # ── Banner ───────────────────────────────────────────────────
    _print_banner(mode, wallet_filter, whale_traders)

    # ── Pre-carga del Token Registry ─────────────────────────────
    logger.info("📚 Pre-cargando Token Registry …")
    count = token_registry.preload()
    logger.info("📚 %d tokens registrados.", count)

    # ── Iniciar Listener ─────────────────────────────────────────
    listener.start()
    time.sleep(1)  # dar un momento para que conecte

    if not listener.is_running:
        logger.error("❌ El listener no pudo arrancar. Revisa la conexión WSS.")
        sys.exit(1)

    # ── Main Loop ────────────────────────────────────────────────
    logger.info("🚀 Bot en marcha. Escuchando whale events (topic-filtered) …")
    print(f"[{_ts()}] LISTENING | mode={mode} | whales={wallet_filter.count} "
          f"| addrs_monitored={len(wallet_filter.all_addresses)} | strategy=TOPIC_FILTERED_ZERO_RPC")
    print()

    signals_count = 0
    last_status = time.time()
    last_loud_status = time.time()
    last_resolution_check = 0.0  # force first check on startup

    try:
        while not _shutdown:
            accum_results = engine.check_positions()
            for ar in accum_results:
                _print_decision_from_result(ar)
                whale_label = ar.whale_label
                trader = whale_traders.get(whale_label)
                if ar.action == "COPIED" and trader and trader.closed_trades:
                    last_trade = trader.closed_trades[-1]
                    trade_logger.log_trade(
                        last_trade, capital_after=trader.available_capital,
                    )
                if ar.action == "LIQUIDATED" and trader and trader.closed_trades:
                    last_trade = trader.closed_trades[-1]
                    trade_logger.log_trade(
                        last_trade, capital_after=trader.available_capital,
                    )

            # ── Market Resolution Reaper ─────────────────────────
            if time.time() - last_resolution_check > config.RESOLUTION_CHECK_INTERVAL_S:
                try:
                    resolved = engine.check_resolved_markets()
                    for r in resolved:
                        _print_resolution(r)
                        whale_label = r.whale_label
                        trader = whale_traders.get(whale_label)
                        if trader and trader.closed_trades:
                            last_trade = trader.closed_trades[-1]
                            trade_logger.log_trade(
                                last_trade,
                                capital_after=trader.available_capital,
                            )
                    if resolved:
                        # Save state immediately after resolutions
                        for trader in whale_traders.values():
                            trader._save_state()
                except Exception as exc:
                    logger.error("Error in resolution reaper: %s", exc)
                last_resolution_check = time.time()

            try:
                sig = signal_queue.get(timeout=1.0)
            except queue.Empty:
                if time.time() - last_status > 30:
                    for label, trader in whale_traders.items():
                        logger.info(f"HEARTBEAT 🐋 {label}: {len(trader.open_positions)} posiciones, capital ${trader.available_capital:.2f}")
                    last_status = time.time()
                if time.time() - last_loud_status > 300:
                    for label, trader in whale_traders.items():
                        logger.info(f"LOUD STATUS 🐋 {label}: {len(trader.open_positions)} posiciones, capital ${trader.available_capital:.2f}")
                    last_loud_status = time.time()
                continue

            signals_count += 1
            result = engine.process_signal(sig)
            _print_accumulation(sig, result)
            if args.once:
                logger.info("--once flag. Saliendo tras primera señal.")
                break

    except KeyboardInterrupt:
        pass
    finally:
        print()
        logger.info("Deteniendo listener …")
        listener.stop()

        # Snapshot CSV before any shutdown processing
        trade_logger.snapshot_csv()

        # ── 1) Run resolution reaper one last time before shutdown ─────
        #    Resolved markets close at $1/$0 (no friction), which is the
        #    correct settlement price. This must happen BEFORE any
        #    orderbook-based liquidation.
        try:
            logger.info("🏁 Running final resolution check before shutdown…")
            resolved = engine.check_resolved_markets()
            for r in resolved:
                _print_resolution(r)
                whale_label = r.whale_label
                trader = whale_traders.get(whale_label)
                if trader and trader.closed_trades:
                    last_trade = trader.closed_trades[-1]
                    trade_logger.log_trade(
                        last_trade, capital_after=trader.available_capital,
                    )
            if resolved:
                logger.info("🏁 Resolved %d positions at settlement.", len(resolved))
        except Exception as exc:
            logger.error("Error in final resolution check: %s", exc)

        # ── 2) Report remaining open positions (do NOT force-liquidate) ──
        #    Positions in still-active markets are preserved on disk so the
        #    bot can resume tracking them after restart. Force-liquidating
        #    at an arbitrary orderbook bid would produce fake PnL.
        for label, trader in whale_traders.items():
            open_pos = list(trader.open_positions.items())
            if open_pos:
                print()
                print("=" * 68)
                print(f"  📦 SHUTDOWN: {len(open_pos)} POSICIONES ABIERTAS PRESERVADAS ({label})")
                print("=" * 68)
                for token_id, pos in open_pos:
                    age_h = (time.time() - pos.entry_time) / 3600
                    print(f"  💤 {pos.side.name} {pos.market_question[:40]} "
                          f"│ entry={pos.entry_price:.4f} │ cost=${pos.cost:.2f} "
                          f"│ age={age_h:.1f}h")
                print(f"\n  Capital libre ({label}): ${trader.available_capital:.2f}")
                print("=" * 68)

        engine.save_whale_positions()  # preserve unresolved positions
        for trader in whale_traders.values():
            trader._save_state()
        logger.info("💾 State saved to disk.")

        engine.print_summary()
        for label, trader in whale_traders.items():
            print(f"SUMMARY 🐋 {label}: {len(trader.open_positions)} posiciones, capital ${trader.available_capital:.2f}")
            trader.print_summary()

        print(f"\n[{_ts()}] SHUTDOWN | signals={signals_count} "
              f"| events_total={listener.events_received} "
              f"| whale_matches={listener.events_matched} "
              f"| mode=TOPIC_FILTERED_ZERO_RPC")

        logger.info(
            "Bot detenido. %d señales procesadas.",
            signals_count,
        )


# ── Display Helpers ──────────────────────────────────────────────────

def _print_banner(
    mode: str,
    wallet_filter: WalletFilter,
    whale_traders: dict[str, PaperTrader],
) -> None:
    """Imprime el banner de inicio."""
    total_capital = sum(t.initial_capital for t in whale_traders.values())
    print()
    print("=" * 68)
    print(f"  🐋 Polymarket WHALE COPY-TRADING Bot")
    print(f"  >> Zero-RPC Architecture (WSS Topic Filter) · Proportional Sizing")
    print(f"  Mode            : {mode.upper()}")
    print(f"  Total Capital   : {total_capital:.2f} USDC ({len(whale_traders)} whales × {total_capital/len(whale_traders):.0f})")
    print(f"  Max Slippage    : {config.COPY_MAX_SLIPPAGE_PCT:.0%}")
    print(f"  ── Per-Whale Capital ──")
    for lbl, trader in whale_traders.items():
        port = config.WHALE_PORTFOLIOS.get(lbl, 0)
        print(f"  {lbl:16s}: €{trader.initial_capital:.0f} capital │ whale portfolio ${port:>12,.0f}")
    print(f"  ── Sizing Proporcional ──")
    print(f"  Conviction min  : {config.COPY_MIN_CONVICTION_PCT:.2%} of whale portfolio")
    print(f"  Multiplier      : {config.COPY_CONVICTION_MULTIPLIER:.0f}x")
    print(f"  Max per trade   : {config.COPY_MAX_POSITION_PCT:.0%} of our capital")
    print(f"  Min trade USD   : ${config.COPY_MIN_TRADE_USD:.2f}")
    print(f"  Batch window    : {config.COPY_BATCH_WINDOW_S:.0f}s (espera fills)")
    print(f"  Price Range     : [{config.COPY_MIN_PRICE}, {config.COPY_MAX_PRICE}]")
    print(f"  WSS Endpoint    : {config.POLYGON_WSS_URL[:50]}…")
    print(f"  ── Strategy (Zero-RPC) ──")
    print(f"  Subscribe       : TOPIC-FILTERED (maker/taker = whale only)")
    print(f"  Filter          : Server-side (Alchemy node)")
    print(f"  RPC calls       : ZERO (no eth_getTransactionByHash)")
    print(f"  Addresses       : {len(wallet_filter.all_addresses)} monitored")
    print(f"  ── Contracts ──")
    print(f"  CTFExchange     : {config.CTF_EXCHANGE_ADDRESS[:20]}…")
    print(f"  NegRiskExchange : {config.NEG_RISK_CTF_EXCHANGE_ADDRESS[:20]}…")
    wallet_filter.print_summary()
    print(f"  Started at      : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 68)
    print()


def _print_heartbeat(
    listener: BlockchainListener,
    engine: CopyTradeEngine,
    whale_traders: dict[str, PaperTrader],
    wallet_filter: WalletFilter,
) -> None:
    """Heartbeat de estado en la consola (determinístico)."""
    total_capital = sum(t.available_capital for t in whale_traders.values())
    accum_info = (
        f" | accum_pending={engine.accumulator.active_count}"
        if engine.accumulator.active_count > 0 else ""
    )
    print(_HEARTBEAT_FMT.format(
        ts=_ts(),
        recv=listener.events_received,
        match=listener.events_matched,
        proc=engine.signals_processed,
        copies=engine.copies_executed,
        pos=len(engine.get_tracked_positions()),
        capital=total_capital,
    ) + accum_info)


def _print_loud_status(
    engine: CopyTradeEngine,
    whale_traders: dict[str, PaperTrader],
) -> None:
    """
    LOUD status display every 5 minutes — impossible to miss.
    Shows open positions, PnL, capital, and copies per whale.
    """
    total_capital = sum(t.available_capital for t in whale_traders.values())
    total_open = sum(len(t.open_positions) for t in whale_traders.values())
    total_closed = sum(len(t.closed_trades) for t in whale_traders.values())
    total_pnl = sum(
        trade.pnl for trader in whale_traders.values()
        for trade in trader.closed_trades
    )
    total_open_cost = sum(
        p.cost for t in whale_traders.values()
        for p in t.open_positions.values()
    )
    total_value = total_capital + total_open_cost

    print()
    print("╔" + "═" * 66 + "╗")
    print(f"║  {'📊 STATUS REPORT':^64s}  ║")
    print(f"║  {_ts():^64s}  ║")
    print("╠" + "═" * 66 + "╣")
    print(f"║  CAPITAL TOTAL:       ${total_capital:>10.2f}                              ║")
    print(f"║  POSICIONES ABIERTAS: {total_open:>10d}    (${total_open_cost:>8.2f} invertidos)     ║")
    print(f"║  TRADES CERRADOS:     {total_closed:>10d}                                ║")
    print(f"║  PNL REALIZADO:       ${total_pnl:>+10.2f}                              ║")
    print(f"║  VALOR TOTAL:         ${total_value:>10.2f}                              ║")
    print(f"║  COPIAS EJECUTADAS:   {engine.copies_executed:>10d}                                ║")

    for label, trader in whale_traders.items():
        if trader.open_positions:
            print("╠" + "═" * 66 + "╣")
            line = f"  🐋 {label} POSICIONES:"
            print(f"║{line:<66s}  ║")
            for tid, pos in trader.open_positions.items():
                q = pos.market_question[:35] if pos.market_question else tid[:35]
                age_h = (time.time() - pos.entry_time) / 3600
                line = f"  {pos.side.name:3s} {q:35s} @ {pos.entry_price:.3f} ${pos.cost:.2f} ({age_h:.1f}h)"
                print(f"║{line:<66s}  ║")

    # Accumulations pending
    n_accum = engine.accumulator.active_count
    if n_accum > 0:
        print("╠" + "═" * 66 + "╣")
        print(f"║  ACUMULACIONES PENDIENTES: {n_accum}                                  ║")

    print("╚" + "═" * 66 + "╝")
    print()


def _print_accumulation(
    sig: WhaleTradeSignal,
    result,
) -> None:
    """Imprime info del fill y estado de acumulación."""
    question = sig.market_question[:40] if sig.market_question else "?"
    print(_ACCUM_FMT.format(
        ts=_ts(),
        fill_n=result.accum_fills,
        whale=sig.whale_label,
        question=question,
        side=sig.action,
        size=sig.size_usd,
        accum=result.accum_usd,
        fills=result.accum_fills,
        conv=result.conviction_pct,
        idle=0.0,  # just registered
        window=config.COPY_BATCH_WINDOW_S,
    ))


def _print_decision_from_result(result) -> None:
    """Imprime decisión de un batch evaluado (COPIED/SKIPPED/etc)."""
    # Only print for meaningful actions, not ACCUMULATED
    if result.action == "ACCUMULATED":
        return

    our_pct = min(
        result.conviction_pct / 100 * config.COPY_CONVICTION_MULTIPLIER,
        config.COPY_MAX_POSITION_PCT,
    )
    our_size = our_pct * 100.0  # per-whale capital (€100 each)

    print(_DECISION_FMT.format(
        ts=_ts(),
        action=result.action,
        whale=result.whale_label,
        side="BUY",  # accumulation evaluated
        asset="?",
        pw=result.whale_price,
        pu=result.our_price,
        slip=result.slippage_pct * 100 if result.slippage_pct else 0.0,
        waccum=result.accum_usd,
        fills=result.accum_fills,
        conv=result.conviction_pct,
        osize=our_size,
    ))
    if result.reason:
        print(f"  └─ {result.reason}")


_RESOLUTION_FMT = (
    "[{ts}] 🏁 RESOLVED | WHALE: {whale} | "
    "ACTION: {action} | TOKEN: {token} | "
    "ENTRY: {entry:.4f} → EXIT: {exit:.4f}"
)


def _print_resolution(result) -> None:
    """Print a market resolution event."""
    emoji = "✅" if "WIN" in result.action else "❌"
    print(f"{emoji} " + _RESOLUTION_FMT.format(
        ts=_ts(),
        whale=result.whale_label,
        action=result.action,
        token=result.token_id[:20] + "…",
        entry=result.whale_price,
        exit=result.our_price,
    ))


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
