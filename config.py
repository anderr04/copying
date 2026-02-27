"""
config.py – Centralised configuration for Polymarket BTC binary bot.

v7 – Multi-Timeframe Momentum
==============================
Key pivot from v6: trade DAILY (and optionally 15-min / 4-hour) markets
instead of 5-min scalping, which was proven structurally unprofitable
(R:R = 0.77, break-even WR 56% vs actual 44%).

Daily markets give R:R ≈ 5:1 (binary swings 30-40%), break-even WR ~16%.
Friction (~3-4%) becomes negligible at that scale.

Supported TIMEFRAME values:
    "daily"   – Bitcoin Up or Down on {Month} {Day}  (slug: bitcoin-up-or-down-on-…)
    "4h"      – BTC 4-hour windows  (slug: btc-updown-4h-{ts})
    "15m"     – BTC 15-minute windows  (slug: btc-updown-15m-{ts})
    "5m"      – BTC 5-minute windows  (slug: btc-updown-5m-{ts})  [legacy v6]
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TRADES_CSV = DATA_DIR / "trades.csv"
LOG_FILE = DATA_DIR / "bot.log"

# ── Polymarket APIs ──────────────────────────────────────────────────
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"

# ── Timeframe Selection ──────────────────────────────────────────────
# Override with env var TIMEFRAME=daily|4h|15m|5m
TIMEFRAME: str = os.getenv("TIMEFRAME", "daily").lower()

# ── Paper-Trading Defaults ───────────────────────────────────────────
INITIAL_CAPITAL: float = float(os.getenv("INITIAL_CAPITAL", "50.0"))

# ── Friction Model ───────────────────────────────────────────────────
SLIPPAGE_PCT: float = float(os.getenv("SLIPPAGE_PCT", "0.005"))

# ── Orderbook Analysis ───────────────────────────────────────────────
IMBALANCE_LEVELS: int = 3
USE_DYNAMIC_SLIPPAGE: bool = os.getenv(
    "USE_DYNAMIC_SLIPPAGE", "true"
).lower() in ("1", "true", "yes")
MAKER_EXIT_TP: bool = False
MAKER_FEE_PCT: float = 0.0

# ── Logging ──────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


# =====================================================================
#  Per-Timeframe Parameter Profiles
# =====================================================================
# Each profile is a dict.  After selecting the active profile, its
# values are injected as module-level variables so existing code that
# reads  config.TRAILING_ACTIVATION  etc. keeps working.

_PROFILES: dict[str, dict] = {
    # ── DAILY ────────────────────────────────────────────────────
    # Huge binary swings (30-40%), friction negligible, R:R ~5:1.
    # Entry: detect trend formation in binary price via momentum.
    # Exit: trailing stop that locks in large gains.
    "daily": dict(
        # Search
        MARKET_SEARCH_QUERIES=[
            "Bitcoin Up or Down",
            "BTC up or down",
        ],
        # Observation — scaled for 17 h market
        POLL_INTERVAL=5.0,            # 5 s between polls
        WAIT_SECONDS=300,             # 5 min baseline (60 samples → stable avg)
        ROLLING_WINDOW=30,            # 30 samples = 150 s lookback
        SPIKE_DEVIATION=0.04,         # 4 % deviation triggers entry
        MIN_RESIDUAL_DEVIATION=0.02,  # 2 % residual after cooldown
        ARMED_COOLDOWN_TICKS=2,       # 2 ticks (~10 s) confirmation
        # Entry Filters
        MAX_ENTRY_PRICE=0.75,         # can buy up to 0.75 (daily moves big)
        MAX_SPREAD=0.06,              # allow wider spreads
        POST_TRADE_COOLDOWN_S=30.0,   # 30 s cooldown between trades
        POSITION_SIZE_PCT=0.15,       # 15 % of capital (better edge → more)
        MIN_TIME_ELAPSED_S=300.0,     # wait ≥ 5 min after market open
        MIN_OB_IMBALANCE=1.20,        # require real directional pressure
        MAX_OB_IMBALANCE=8.00,        # higher cap for deep books
        MIN_SPIKE_VELOCITY_ABS=0.005, # slower moves still valid on daily
        # Momentum Exit Engine — wide breathing room for 30% swings
        TRAILING_ACTIVATION=0.08,     # activate after +8 % (need real move)
        TRAILING_DISTANCE=0.06,       # trail 6 % behind peak (allow pullbacks)
        VELOCITY_WINDOW=4,            # 4 ticks (~20 s)
        STALL_VELOCITY_THRESHOLD=0.0003,  # tighter stall for slower market
        STALL_TICKS_TO_EXIT=6,        # 6 ticks stalling (~30 s)
        ACCEL_EXIT_THRESHOLD=-0.0008, # softer accel exit
        # Emergency Exits
        SPREAD_EMERGENCY=0.12,        # wider tolerance
        OB_COLLAPSE_THRESHOLD=0.20,   # protect gains if OB collapses
        HARD_STOP_LOSS_USD=4.00,      # wider USD stop (bigger positions)
        HARD_STOP_LOSS_PCT=0.12,      # 12 % hard stop (room to breathe)
        # Time Management
        TIME_STOP_BEFORE_CLOSE=600,   # exit 10 min before market close
        PRE_CLOSE_EXIT_SECONDS=300,   # no entry in last 5 min
        HARD_STOP_SECONDS=120,        # force exit 2 min before close
        MIN_PROFIT=0.01,
        MAX_HOLD_SECONDS=7200,        # max 2 h per trade
    ),

    # ── 4 HOUR ──────────────────────────────────────────────────
    # Moderate binary swings, thinner liquidity (spread ~4%).
    "4h": dict(
        MARKET_SEARCH_QUERIES=[
            "Bitcoin Up or Down",
            "BTC up or down",
        ],
        POLL_INTERVAL=4.0,
        WAIT_SECONDS=60,
        ROLLING_WINDOW=15,
        SPIKE_DEVIATION=0.05,
        MIN_RESIDUAL_DEVIATION=0.03,
        ARMED_COOLDOWN_TICKS=2,
        MAX_ENTRY_PRICE=0.70,
        MAX_SPREAD=0.08,              # 4h OB is thinner
        POST_TRADE_COOLDOWN_S=20.0,
        POSITION_SIZE_PCT=0.12,
        MIN_TIME_ELAPSED_S=40.0,
        MIN_OB_IMBALANCE=1.00,        # softer (thin book)
        MAX_OB_IMBALANCE=10.00,
        MIN_SPIKE_VELOCITY_ABS=0.01,
        TRAILING_ACTIVATION=0.04,
        TRAILING_DISTANCE=0.025,
        VELOCITY_WINDOW=4,
        STALL_VELOCITY_THRESHOLD=0.001,
        STALL_TICKS_TO_EXIT=4,
        ACCEL_EXIT_THRESHOLD=-0.002,
        SPREAD_EMERGENCY=0.15,
        OB_COLLAPSE_THRESHOLD=0.20,
        HARD_STOP_LOSS_USD=2.50,
        HARD_STOP_LOSS_PCT=0.08,
        TIME_STOP_BEFORE_CLOSE=300,
        PRE_CLOSE_EXIT_SECONDS=180,
        HARD_STOP_SECONDS=60,
        MIN_PROFIT=0.008,
        MAX_HOLD_SECONDS=3600,
    ),

    # ── 15 MIN ──────────────────────────────────────────────────
    # Intermediate: bigger than 5-min but still short.
    "15m": dict(
        MARKET_SEARCH_QUERIES=[
            "Bitcoin Up or Down",
            "BTC up or down",
            "BTC 15 minute",
        ],
        POLL_INTERVAL=3.0,
        WAIT_SECONDS=30,
        ROLLING_WINDOW=12,
        SPIKE_DEVIATION=0.05,
        MIN_RESIDUAL_DEVIATION=0.025,
        ARMED_COOLDOWN_TICKS=1,
        MAX_ENTRY_PRICE=0.68,
        MAX_SPREAD=0.06,
        POST_TRADE_COOLDOWN_S=10.0,
        POSITION_SIZE_PCT=0.12,
        MIN_TIME_ELAPSED_S=30.0,
        MIN_OB_IMBALANCE=1.15,
        MAX_OB_IMBALANCE=6.00,
        MIN_SPIKE_VELOCITY_ABS=0.02,
        TRAILING_ACTIVATION=0.035,
        TRAILING_DISTANCE=0.02,
        VELOCITY_WINDOW=3,
        STALL_VELOCITY_THRESHOLD=0.0015,
        STALL_TICKS_TO_EXIT=3,
        ACCEL_EXIT_THRESHOLD=-0.002,
        SPREAD_EMERGENCY=0.10,
        OB_COLLAPSE_THRESHOLD=0.25,
        HARD_STOP_LOSS_USD=2.00,
        HARD_STOP_LOSS_PCT=0.06,
        TIME_STOP_BEFORE_CLOSE=120,
        PRE_CLOSE_EXIT_SECONDS=60,
        HARD_STOP_SECONDS=30,
        MIN_PROFIT=0.006,
        MAX_HOLD_SECONDS=600,
    ),

    # ── 5 MIN (legacy v6) ──────────────────────────────────────
    "5m": dict(
        MARKET_SEARCH_QUERIES=[
            "Bitcoin Up or Down",
            "BTC up or down",
            "BTC 5 minute",
            "Bitcoin 5-minute",
            "BTC",
        ],
        POLL_INTERVAL=2.0,
        WAIT_SECONDS=15,
        ROLLING_WINDOW=8,
        SPIKE_DEVIATION=0.06,
        MIN_RESIDUAL_DEVIATION=0.03,
        ARMED_COOLDOWN_TICKS=1,
        MAX_ENTRY_PRICE=0.65,
        MAX_SPREAD=0.05,
        POST_TRADE_COOLDOWN_S=4.0,
        POSITION_SIZE_PCT=0.10,
        MIN_TIME_ELAPSED_S=20.0,
        MIN_OB_IMBALANCE=1.20,
        MAX_OB_IMBALANCE=5.00,
        MIN_SPIKE_VELOCITY_ABS=0.10,
        TRAILING_ACTIVATION=0.03,
        TRAILING_DISTANCE=0.02,
        VELOCITY_WINDOW=3,
        STALL_VELOCITY_THRESHOLD=0.002,
        STALL_TICKS_TO_EXIT=3,
        ACCEL_EXIT_THRESHOLD=-0.003,
        SPREAD_EMERGENCY=0.10,
        OB_COLLAPSE_THRESHOLD=0.25,
        HARD_STOP_LOSS_USD=1.50,
        HARD_STOP_LOSS_PCT=0.05,
        TIME_STOP_BEFORE_CLOSE=60,
        PRE_CLOSE_EXIT_SECONDS=30,
        HARD_STOP_SECONDS=15,
        MIN_PROFIT=0.005,
        MAX_HOLD_SECONDS=180,
    ),
}


# =====================================================================
#  Inject active profile as module-level variables
# =====================================================================
_active = _PROFILES.get(TIMEFRAME)
if _active is None:
    raise ValueError(
        f"Unknown TIMEFRAME='{TIMEFRAME}'. "
        f"Valid: {', '.join(_PROFILES.keys())}"
    )

# Set each key as a module global so  config.TRAILING_ACTIVATION  works.
_g = globals()
for _k, _v in _active.items():
    _g[_k] = _v

# Explicit type hints so IDE/type-checkers see the variables:
MARKET_SEARCH_QUERIES: list[str]
POLL_INTERVAL: float
WAIT_SECONDS: int
ROLLING_WINDOW: int
SPIKE_DEVIATION: float
MIN_RESIDUAL_DEVIATION: float
ARMED_COOLDOWN_TICKS: int
MAX_ENTRY_PRICE: float
MAX_SPREAD: float
POST_TRADE_COOLDOWN_S: float
POSITION_SIZE_PCT: float
MIN_TIME_ELAPSED_S: float
MIN_OB_IMBALANCE: float
MAX_OB_IMBALANCE: float
MIN_SPIKE_VELOCITY_ABS: float
TRAILING_ACTIVATION: float
TRAILING_DISTANCE: float
VELOCITY_WINDOW: int
STALL_VELOCITY_THRESHOLD: float
STALL_TICKS_TO_EXIT: int
ACCEL_EXIT_THRESHOLD: float
SPREAD_EMERGENCY: float
OB_COLLAPSE_THRESHOLD: float
HARD_STOP_LOSS_USD: float
HARD_STOP_LOSS_PCT: float
TIME_STOP_BEFORE_CLOSE: int
PRE_CLOSE_EXIT_SECONDS: int
HARD_STOP_SECONDS: int
MIN_PROFIT: float
MAX_HOLD_SECONDS: int


# =====================================================================
#  Copy-Trading Configuration (Hybrid Web3 + CLOB)
# =====================================================================

# ── Polygon WebSocket (Zero-RPC) ────────────────────────────────────
# Solo WSS. NO se usa HTTP/RPC (ahorro masivo de Alchemy CU).
# Configurar en .env:
#   POLYGON_WSS_URL=wss://polygon-mainnet.g.alchemy.com/v2/TU_API_KEY
POLYGON_WSS_URL: str = os.getenv(
    "POLYGON_WSS_URL",
    "wss://polygon-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_API_KEY",
)
# DEPRECATED: POLYGON_RPC_URL ya NO se usa (zero-RPC architecture).
# Se mantiene por compatibilidad pero ningún módulo lo consume.
POLYGON_RPC_URL: str = os.getenv(
    "POLYGON_RPC_URL",
    "https://polygon-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_API_KEY",
)

# ── CTFExchange Contracts (Polygon Mainnet) ──────────────────────────
# Polymarket usa dos contratos de exchange para matchear órdenes:
#   CTFExchange      – mercados estándar
#   NegRiskCTFExchange – mercados con riesgo negativo (multi-outcome)
CTF_EXCHANGE_ADDRESS: str = os.getenv(
    "CTF_EXCHANGE_ADDRESS",
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
)
NEG_RISK_CTF_EXCHANGE_ADDRESS: str = os.getenv(
    "NEG_RISK_CTF_EXCHANGE_ADDRESS",
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",
)

# ── Target Whale Wallets (Static – Zero-RPC) ────────────────────────
# Formato .env:
#   WHALE_WALLETS=Label:EOA[:proxy1,proxy2];Label2:EOA2[:proxy3]
#
# Ejemplos:
#   Solo EOA:     WHALE_WALLETS=DrPufferfish:0xdb27bf...
#   Con proxies:  WHALE_WALLETS=DrPufferfish:0xdb27bf...:0xproxy1,0xproxy2
#   Multi-whale:  WHALE_WALLETS=DrPufferfish:0xdb27...:0xproxyA;LucasMeow:0xabc...
#
# Las proxy wallets se usan JUNTO con la EOA para filtrar por topics
# en la suscripción WSS. Alchemy solo enviará eventos donde maker o
# taker sea una de estas direcciones → CERO consumo de CU adicional.
#
WHALE_WALLETS: dict[str, str] = {}     # label → EOA
WHALE_PROXIES: dict[str, list[str]] = {}  # label → [proxy1, proxy2, …]

# Carga estática desde variable de entorno (separador = ";")
_whale_env = os.getenv("WHALE_WALLETS", "")
if _whale_env:
    for _entry in _whale_env.split(";"):
        _entry = _entry.strip()
        if not _entry:
            continue
        _parts = _entry.split(":", 2)  # max 3 partes: label, eoa, proxies
        if len(_parts) < 2:
            continue
        _lbl = _parts[0].strip()
        _eoa = _parts[1].strip()
        if _lbl and _eoa:
            WHALE_WALLETS[_lbl] = _eoa
            if len(_parts) == 3 and _parts[2].strip():
                _proxy_list = [p.strip() for p in _parts[2].split(",") if p.strip()]
                if _proxy_list:
                    WHALE_PROXIES[_lbl] = _proxy_list

# Fallback: si no hay nada en .env, usar default hardcoded
if not WHALE_WALLETS:
    WHALE_WALLETS = {
        "DrPufferfish": "0xdb27bf2ac5d428a9c63dbc914611036855a6c56e",
    }

# ── Copy-Trading Parameters ──────────────────────────────────────────

# --- Portfolio proporcional (whale → nosotros) ---
# Tamaño estimado del portfolio de cada whale (USD).
# IMPORTANTE: Usamos POSICIONES ACTIVAS, no ganancias totales.
# DrPufferfish tiene $6.24M de profit lifetime, pero solo $645K desplegados ahora.
# La convicción mide: "¿qué % de su capital ACTIVO arriesga en este trade?"
# Si apuesta $8K teniendo $645K activos → conv = 1.24% → señal fuerte.
# Si usáramos $6.24M → conv = 0.13% → señal invisible (incorrecto).
#
# Actualizar estos valores periódicamente mirando sus perfiles en Polymarket.
WHALE_PORTFOLIOS: dict[str, float] = {
    "DrPufferfish": 1_000_000.0,   # Portfolio estimado $1M
    "LucasMeow":     564_000.0,   # Portfolio estimado $564K
    "Tsybka":        350_000.0,   # Portfolio estimado $350K
}
# Valor por defecto si un whale no está en WHALE_PORTFOLIOS.
DEFAULT_WHALE_PORTFOLIO_USD: float = float(
    os.getenv("DEFAULT_WHALE_PORTFOLIO_USD", "1000000.0")
)

# Convicción mínima (% del portfolio del whale) para considerar un trade.
# Ej: 0.001 = 0.1%  →  para un whale de $6M = trades ≥ $6,000
# Todo lo que esté por debajo se considera ruido y se ignora.
COPY_MIN_CONVICTION_PCT: float = float(
    os.getenv("COPY_MIN_CONVICTION_PCT", "0.001")
)

# Multiplicador de convicción.
# Escala la convicción del whale a nuestro bankroll:
#   our_size = conviction × multiplier × our_capital
# Ej: whale pone 1% de su portfolio → nosotros ponemos 1% × 30 = 30% del nuestro.
COPY_CONVICTION_MULTIPLIER: float = float(
    os.getenv("COPY_CONVICTION_MULTIPLIER", "30.0")
)

# Máximo % de capital por operación individual (límite de riesgo).
COPY_MAX_POSITION_PCT: float = float(
    os.getenv("COPY_MAX_POSITION_PCT", "0.40")   # 40% = $20 de $50
)

# Mínimo absoluto de trade en USD (Polymarket tiene mínimos prácticos).
COPY_MIN_TRADE_USD: float = float(
    os.getenv("COPY_MIN_TRADE_USD", "1.0")
)

# --- Filtros de precio y slippage (sin cambios) ---

# Umbral máximo de slippage: si P_u > P_b × (1 + SLIPPAGE), abortar.
COPY_MAX_SLIPPAGE_PCT: float = float(os.getenv("COPY_MAX_SLIPPAGE_PCT", "0.05"))  # 5% cap (dynamic adjusts down at high prices)

# (LEGACY — mantenido para compatibilidad, pero el filtro principal
#  ahora es COPY_MIN_CONVICTION_PCT.  Si quieres un floor absoluto
#  además del proporcional, ponlo aquí.)
COPY_MIN_WHALE_SIZE_USD: float = float(os.getenv("COPY_MIN_WHALE_SIZE_USD", "100.0"))

# Tamaño de posición fijo (LEGACY — sustituido por convicción proporcional).
COPY_POSITION_SIZE_PCT: float = float(os.getenv("COPY_POSITION_SIZE_PCT", "0.10"))

# Rango de precios aceptable para copiar (evitar extremos sin valor).
COPY_MAX_PRICE: float = float(os.getenv("COPY_MAX_PRICE", "0.95"))
COPY_MIN_PRICE: float = float(os.getenv("COPY_MIN_PRICE", "0.05"))

# Modo de ejecución: "paper" (simulado) o "live" (real vía py-clob-client).
COPY_MODE: str = os.getenv("COPY_MODE", "paper")

# ── Accumulation / Batch Window ──────────────────────────────────────
# Segundos sin nuevos fills para considerar que la ráfaga de un whale
# terminó y evaluar el total acumulado.
# Ej: DrPufferfish hace 8 fills en 60s → esperamos 120s más → evaluamos total.
COPY_BATCH_WINDOW_S: float = float(os.getenv("COPY_BATCH_WINDOW_S", "120.0"))

# Timeout para limpiar acumulaciones sin actividad (segundos).
COPY_ACCUM_STALE_S: float = float(os.getenv("COPY_ACCUM_STALE_S", "3600.0"))

# ── Token Registry ───────────────────────────────────────────────────
# Intervalo de refresco del cache de tokens (segundos).
TOKEN_REGISTRY_REFRESH_S: float = float(os.getenv("TOKEN_REGISTRY_REFRESH_S", "300"))
# Número máximo de eventos a cargar al inicio.
TOKEN_REGISTRY_PRELOAD_LIMIT: int = int(os.getenv("TOKEN_REGISTRY_PRELOAD_LIMIT", "200"))

# ── Web3 Listener (Zero-RPC) ─────────────────────────────────────────
# Delay entre reconexiones WSS (con backoff exponencial).
WEB3_RECONNECT_DELAY_S: float = float(os.getenv("WEB3_RECONNECT_DELAY_S", "5.0"))
# DEPRECATED: WEB3_POLL_FALLBACK_S ya no se usa (no hay HTTP polling).
WEB3_POLL_FALLBACK_S: float = float(os.getenv("WEB3_POLL_FALLBACK_S", "2.0"))

