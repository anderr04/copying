"""
config.py – Centralised configuration for Polymarket Copy-Trading Bot.

Manages all parameters for:
    • WebSocket listener (Polygon WSS)
    • Whale wallet tracking & proxy detection
    • Copy-trading sizing (conviction-based proportional)
    • Paper-trading simulation
    • Market resolution reaper
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

# ── Legacy sizing (used by PaperTrader.open_trade) ───────────────────
POSITION_SIZE_PCT: float = float(os.getenv("POSITION_SIZE_PCT", "0.10"))




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

# Log de wallets cargadas al arrancar.
import logging as _logging
_cfg_logger = _logging.getLogger(__name__)
_cfg_logger.info("Loaded %d whale wallets: %s", len(WHALE_WALLETS), list(WHALE_WALLETS.keys()))

# ── Copy-Trading Parameters ──────────────────────────────────────────

# --- Portfolio proporcional (whale → nosotros) ---
# Tamaño estimado del portfolio de cada whale (USD).
# Solo incluimos wallets con portfolio conocido.
# Los wallets del scanner usan DEFAULT_WHALE_PORTFOLIO_USD ($500K conservador)
# porque no conocemos su portfolio real — mejor sub-estimar para no sobre-copiar.
#
# La convicción mide: "¿qué % de su capital ACTIVO arriesga en este trade?"
# Si apuesta $8K teniendo $645K activos → conv = 1.24% → señal fuerte.
WHALE_PORTFOLIOS: dict[str, float] = {
    "swisstony":       2_000_000.0,   # $2M (Polymarket: $2.3M activos)
    "RN1":               400_000.0,   # $400K (Polymarket: $444.5K activos)
    "kch123":            830_000.0,   # $830K activos (Grok: $828K)
    "432614799197":       86_000.0,   # $86.5K posiciones reales (Polymarket)
    "ImJustKen":         650_000.0,   # $650K activos (Grok: $656K)
    "Countryside":       400_000.0,   # $398.2K posiciones reales (Polymarket)
    "aenews2":           240_000.0,   # $240K activos (Grok: $241K)
    # Macks22, NicholasWickerson, rdba → DEFAULT_WHALE_PORTFOLIO_USD ($500K)
}
# Valor por defecto para wallets sin portfolio conocido.
# $500K es conservador: evita trades proporcionalmente demasiado grandes.
DEFAULT_WHALE_PORTFOLIO_USD: float = float(
    os.getenv("DEFAULT_WHALE_PORTFOLIO_USD", "500000.0")
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
# Ej: whale pone 1% de su portfolio → nosotros ponemos 1% × 10 = 10% del nuestro.
# Con $100 de capital:
#   - trade mínimo (0.1% conv) → $1.00 → cómodo, muchos trades posibles
#   - trade agresivo (1% conv) → $10.00 → razonable
#   - trade extremo (2% conv)  → $20.00 → tocando el techo de COPY_MAX_POSITION_PCT
# Anteriormente 30x: agotaba el capital en 2-3 trades de alta convicción.
COPY_CONVICTION_MULTIPLIER: float = float(
    os.getenv("COPY_CONVICTION_MULTIPLIER", "10.0")
)

# Máximo % de capital por operación individual (límite de riesgo).
# 20% → nunca más de $20 en un solo trade (sobre $100 capital).
# Protege contra All-In de ballena ultra-sniper o un súbito spike de convicción.
COPY_MAX_POSITION_PCT: float = float(
    os.getenv("COPY_MAX_POSITION_PCT", "0.20")   # 20% = $20 de $100
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

# ── Market Resolution Reaper ────────────────────────────────────────
# Intervalo entre checks de resolución de mercados (segundos).
# El reaper consulta la Gamma API para cada posición abierta y cierra
# automáticamente las que estén en mercados resueltos (closed=true).
RESOLUTION_CHECK_INTERVAL_S: float = float(
    os.getenv("RESOLUTION_CHECK_INTERVAL_S", "120.0")
)

# ── Web3 Listener (Zero-RPC) ─────────────────────────────────────────
# Delay entre reconexiones WSS (con backoff exponencial).
WEB3_RECONNECT_DELAY_S: float = float(os.getenv("WEB3_RECONNECT_DELAY_S", "5.0"))
# DEPRECATED: WEB3_POLL_FALLBACK_S ya no se usa (no hay HTTP polling).
WEB3_POLL_FALLBACK_S: float = float(os.getenv("WEB3_POLL_FALLBACK_S", "2.0"))

