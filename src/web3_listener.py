"""
web3_listener.py – Escucha FILTRADA de eventos OrderFilled vía WebSocket.

Arquitectura ZERO-RPC
---------------------
- 100% WebSocket (WSS) – CERO llamadas HTTP/RPC.
- Filtrado server-side por topics: Alchemy solo envía eventos
  donde maker (topic[2]) o taker (topic[3]) es una ballena conocida.
- Sin descubrimiento dinámico de proxies (config estática).
- Sin fallback HTTP polling.
- Sin eth_getTransactionByHash.
- Sin import de web3 ni aiohttp.

Suscripciones (2 en total):
    Sub 1: topic[2] = [whale_addresses_padded]  → whale como maker
    Sub 2: topic[3] = [whale_addresses_padded]  → whale como taker

Compute Units estimadas:
    - eth_subscribe: 10 CU × 2 suscripciones = 20 CU total (una vez)
    - Logs recibidos: 0 CU (push del nodo, no polling)
    - Resultado: ~0.01% del consumo anterior (de ~15 evt/seg a ~0-5 evt/DÍA)

Deduplicación:
    El mismo evento puede llegar por ambas suscripciones si el whale es
    maker Y taker simultáneamente.  Se deduplicar por (tx_hash, log_index).
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from datetime import datetime
from typing import Optional

import config
from src.event_decoder import EventDecoder, WhaleTradeSignal
from src.token_registry import TokenRegistry
from src.wallet_filter import WalletFilter

logger = logging.getLogger(__name__)


# ── Formato determinístico de log ────────────────────────────────
_LOG_FMT = (
    "[{ts}] MATCH WHALE: {whale} | MARKET: {question} | "
    "SIDE: {side} | ASSET: {asset} | PRECIO: {price:.6f} | "
    "WHALE_SIZE: ${size:.2f} | CONV: {conv:.4f}% | OUR_SIZE: ${our_size:.2f}"
)

_LATENCY_FMT = (
    "[{ts}] LATENCY | decode={decode_ms:.1f}ms | "
    "total={total_ms:.1f}ms | "
    "events_seen={seen} | events_matched={matched}"
)


def _now_local_str() -> str:
    """Timestamp local ISO sin microsegundos."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class BlockchainListener:
    """
    Listener Zero-RPC: filtrado server-side por topics en Alchemy WSS.
    Solo recibe eventos donde maker o taker es una ballena conocida.
    Consumo de Alchemy: ~20 CU al suscribirse + 0 CU por evento recibido.
    """

    def __init__(
        self,
        decoder: EventDecoder,
        wallet_filter: WalletFilter,
        signal_queue: queue.Queue,
        token_registry: TokenRegistry | None = None,
        wss_url: str = config.POLYGON_WSS_URL,
        exchange_addresses: list[str] | None = None,
        reconnect_delay: float = config.WEB3_RECONNECT_DELAY_S,
    ):
        self.decoder = decoder
        self.wallet_filter = wallet_filter
        self.token_registry = token_registry
        self.signal_queue = signal_queue
        self.wss_url = wss_url
        self.exchange_addresses = exchange_addresses or [
            config.CTF_EXCHANGE_ADDRESS,
            config.NEG_RISK_CTF_EXCHANGE_ADDRESS,
        ]
        self.reconnect_delay = reconnect_delay

        self._shutdown = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_block: int = 0
        self._events_received: int = 0
        self._events_matched: int = 0

        # Dedup: (tx_hash, log_index) → prevenir doble procesamiento
        # (un mismo evento puede llegar de la sub-maker Y sub-taker)
        self._seen_events: set[tuple[str, int]] = set()
        self._seen_max: int = 10_000

    # ── Stats ────────────────────────────────────────────────────

    @property
    def events_received(self) -> int:
        return self._events_received

    @property
    def events_matched(self) -> int:
        return self._events_matched

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── Start / Stop ─────────────────────────────────────────────

    def start(self) -> None:
        """Inicia el listener en un thread separado."""
        if self.wallet_filter.is_empty:
            logger.error(
                "❌ No hay wallets configuradas. No se puede iniciar el listener."
            )
            return

        whale_topics = self.wallet_filter.addresses_as_topics()
        if not whale_topics:
            logger.error("❌ No hay direcciones para filtrar por topics.")
            return

        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="BlockchainListener",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "🔗 Blockchain listener iniciado (thread=%s, exchanges=%d, "
            "whale_addresses=%d, mode=TOPIC_FILTERED).",
            self._thread.name,
            len(self.exchange_addresses),
            len(whale_topics),
        )

    def stop(self) -> None:
        """Detiene el listener."""
        self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=10)
            logger.info("Blockchain listener detenido.")

    # ── Thread entry point ───────────────────────────────────────

    def _run_loop(self) -> None:
        """Entry point del thread: corre el event loop asyncio."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:
            logger.error("Error fatal en listener thread: %s", exc, exc_info=True)
        finally:
            loop.close()

    async def _async_main(self) -> None:
        """Loop principal async con reconexión exponencial."""
        attempt = 0
        while not self._shutdown.is_set():
            try:
                await self._listen_websocket()
                attempt = 0  # reset on clean disconnect
            except Exception as exc:
                attempt += 1
                delay = min(self.reconnect_delay * (2 ** min(attempt, 6)), 120)
                logger.warning(
                    "WSS desconectado: %s. Reconectando en %.1f s (intento #%d)…",
                    exc, delay, attempt,
                )
                await asyncio.sleep(delay)

    # ── WebSocket Subscription (TOPIC-FILTERED) ──────────────────

    async def _listen_websocket(self) -> None:
        """
        Suscripción FILTRADA: 2 suscripciones con topic filtering.

        Sub 1: topic[0]=OrderFilled, topic[2]=[whale_addrs] → maker
        Sub 2: topic[0]=OrderFilled, topic[3]=[whale_addrs] → taker

        Alchemy filtra server-side. Solo recibimos eventos de nuestras
        ballenas. Consumo ~0 CU en estado estable.
        """
        import websockets

        topic0 = self.decoder.topic0_hex
        whale_topics = self.wallet_filter.addresses_as_topics()

        # Dos suscripciones: maker-filter y taker-filter
        # address = array de ambos contratos exchange
        filters = [
            {
                "address": self.exchange_addresses,
                "topics": [topic0, None, whale_topics],
            },
            {
                "address": self.exchange_addresses,
                "topics": [topic0, None, None, whale_topics],
            },
        ]
        sub_labels = ["MAKER", "TAKER"]

        logger.info("🌐 Conectando a WSS: %s …", self.wss_url[:60] + "…")

        async with websockets.connect(
            self.wss_url,
            ping_interval=30,
            ping_timeout=20,
            close_timeout=10,
            max_size=2**20,
        ) as ws:
            logger.info("✅ WebSocket conectado.")

            sub_ids: list[str] = []
            for i, filt in enumerate(filters):
                sub_msg = {
                    "jsonrpc": "2.0",
                    "id": i + 1,
                    "method": "eth_subscribe",
                    "params": ["logs", filt],
                }
                await ws.send(json.dumps(sub_msg))
                resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                resp = json.loads(resp_raw)
                sub_id = resp.get("result")
                if sub_id:
                    sub_ids.append(sub_id)
                    logger.info(
                        "📡 Sub %s → ID=%s | %d whale addrs × %d contracts",
                        sub_labels[i], sub_id,
                        len(whale_topics), len(self.exchange_addresses),
                    )
                else:
                    logger.warning(
                        "⚠️ Fallo en suscripción %s: %s",
                        sub_labels[i], resp.get("error", resp),
                    )

            logger.info(
                "📡 %d suscripciones activas (TOPIC-FILTERED, zero-RPC). "
                "Esperando whale trades …",
                len(sub_ids),
            )

            # ── Receive Loop ─────────────────────────────────────
            while not self._shutdown.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    msg = json.loads(raw)

                    if msg.get("method") == "eth_subscription":
                        log_data = msg.get("params", {}).get("result")
                        if log_data:
                            self._process_log(log_data)

                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("WebSocket cerrado por el servidor.")
                    raise

    # ── Log Processing (Zero-RPC) ────────────────────────────────

    def _process_log(self, log_data: dict) -> None:
        """
        Procesa un log FILTRADO por topics (garantizado whale event).

        Flujo:
            1. Decodifica el evento OrderFilled.
            2. Dedup por (tx_hash, log_index).
            3. Identifica qué whale matcheó (maker o taker).
            4. Genera señal, enriquece con TokenRegistry.
            5. Envía al queue + log determinístico.

        ZERO llamadas RPC. Todo es local.
        """
        t_recv = time.perf_counter()
        self._events_received += 1

        # ── 1. Decodificar ───────────────────────────────────────
        event = self.decoder.decode_log(log_data)
        if event is None:
            return

        # ── 2. Dedup (mismo evento puede venir de sub-maker Y sub-taker) ─
        event_key = (event.tx_hash, event.log_index)
        if event_key in self._seen_events:
            return
        self._seen_events.add(event_key)
        if len(self._seen_events) > self._seen_max:
            to_remove = list(self._seen_events)[: self._seen_max // 2]
            for k in to_remove:
                self._seen_events.discard(k)

        # ── 3. Actualizar _last_block ────────────────────────────
        if event.block_number > self._last_block:
            self._last_block = event.block_number

        # ── 4. Identificar whale (match garantizado por el server) ─
        match = self.wallet_filter.match_event(event.maker, event.taker)
        if not match:
            # No debería ocurrir con topic filtering, pero defensivo
            logger.debug(
                "Evento sin match (inesperado con topic filter): tx=%s",
                event.tx_hash[:18],
            )
            return

        whale_address, whale_label, whale_role = match

        t_match = time.perf_counter()
        decode_ms = (t_match - t_recv) * 1000
        self._events_matched += 1

        # ── 5. Generar señal ─────────────────────────────────────
        signal = self.decoder.to_whale_signal(
            event, whale_address, whale_label,
            whale_role=whale_role,
        )
        if signal is None:
            logger.debug("No se pudo generar señal de tx=%s", event.tx_hash)
            return

        # ── 6. Enriquecer con TokenRegistry ──────────────────────
        if self.token_registry:
            info = self.token_registry.lookup(signal.token_id)
            if info:
                signal.outcome = info.outcome
                signal.condition_id = info.condition_id
                signal.market_question = info.question

        # ── 7. Enviar al engine ──────────────────────────────────
        self.signal_queue.put(signal)

        # ── 8. Log determinístico ────────────────────────────────
        ts = _now_local_str()

        _whale_port = getattr(config, "WHALE_PORTFOLIOS", {}).get(
            whale_label,
            getattr(config, "DEFAULT_WHALE_PORTFOLIO_USD", 1_000_000),
        )
        _conv_pct = (signal.size_usd / _whale_port * 100) if _whale_port > 0 else 0.0
        _multiplier = getattr(config, "COPY_CONVICTION_MULTIPLIER", 30.0)
        _max_pos = getattr(config, "COPY_MAX_POSITION_PCT", 0.40)
        _our_pct = min(_conv_pct / 100 * _multiplier, _max_pos)
        _our_size = _our_pct * getattr(config, "INITIAL_CAPITAL", 50.0)

        _question_display = (
            signal.market_question[:50]
            if signal.market_question
            else (
                signal.token_id[:20] + "…"
                if len(signal.token_id) > 20
                else signal.token_id
            )
        )

        print(
            _LOG_FMT.format(
                ts=ts,
                whale=f"{whale_label} ({whale_address[:10]}…)",
                question=_question_display,
                side=signal.action,
                asset=signal.outcome.upper() if signal.outcome else "UNKNOWN",
                price=signal.price,
                size=signal.size_usd,
                conv=_conv_pct,
                our_size=_our_size,
            )
        )
        print(
            _LATENCY_FMT.format(
                ts=ts,
                decode_ms=decode_ms,
                total_ms=decode_ms,
                seen=self._events_received,
                matched=self._events_matched,
            )
        )

        logger.info(
            "🐋 WHALE TRADE │ %s │ role=%s │ %s │ "
            "price=%.6f │ $%.2f │ latency=%.1fms │ tx=%s",
            whale_label,
            whale_role,
            signal.action,
            signal.price,
            signal.size_usd,
            decode_ms,
            event.tx_hash[:18] + "…",
        )
