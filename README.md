# Polymarket Whale Copy-Trading Bot

> **Architecture v2 — Zero-RPC & Proportional Sizing**
> Bot avanzado de copy-trading algorítmico diseñado para Polymarket. Sigue en tiempo real a decenas de "Smart Money" wallets utilizando una arquitectura WSS de coste RPC cero y un sistema matemático de convicción proporcional.

---

## 🏗 Arquitectura del Sistema

El bot está diseñado para ser extremadamente eficiente y 100% resistente al ruido algorítmico, utilizando dos pilares fundamentales:

### 1. Zero-RPC (Topic-Filtered WebSocket)
En lugar de descargar cada bloque de la blockchain de Polygon y usar miles de peticiones `eth_getTransactionByHash` para decodificar quién opera, el bot utiliza un filtro a nivel de nodo de Alchemy.
- Se suscribe a los eventos del `CTFExchange` y `NegRiskExchange`.
- Filtra instantáneamente *solo* los eventos donde el Maker o Taker coinciden con nuestra lista de `WHALE_WALLETS`.
- **Resultado**: Cero consumo de créditos RPC repetitivos, latencia < 1ms, y capacidad para vigilar docenas de carteras simultáneamente de forma gratuita.

### 2. Proportional Sizing & Conviction Tracking
No seguimos a ciegas. El bot evalúa el tamaño del trade del whale relativo a su portfolio estimado total para medir su **"Convicción"**.
- Si un whale con $1M mete $1,000 → Convicción del 0.1%
- **Filtro de Ruido**: Todo trade con convicción < 0.1% es descartado (Market Making, exploración, farmeo).
- **Ejecución Proporcional**: Si supera el filtro, el bot escala la convicción a nuestro capital (ej. multiplicador 30x).
- **Agrupación (Batching)**: Agrupa múltiples fills pequeños del whale durante una ventana de 120s antes de ejecutar la copia, evitando el problema del "Scale-In".

---

## 🛠 Instalación y Configuración

### 1. Preparar el entorno

```bash
# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Instalar dependencias
pip install -r requirements.txt
```

### 2. Configurar Variables de Entorno (`.env`)

Renombra `.env_example` a `.env` y añade tus credenciales. La configuración de las wallets sigue este formato: `NombreWallet:0xDireccionEVM;OtraWallet:0xDireccion...`

```env
# ── Alchemy (Polygon) ──
POLYGON_WSS_URL=wss://polygon-mainnet.g.alchemy.com/v2/TU_API_KEY_AQUI

# ── Whale Wallets ──
# Separa múltiples wallets con punto y coma (;)
WHALE_WALLETS="DrPufferfish:0xdb27bf2ac5d428a9c63dbc914611036855a6c56e;swisstony:0x204f72f35326db932158cba6adff0b9a1da95e14"

# ── Copy-Trading Parameters ──
COPY_MAX_SLIPPAGE_PCT=0.05
COPY_POSITION_SIZE_PCT=0.10
COPY_MIN_WHALE_SIZE_USD=100.0
COPY_MODE=paper

INITIAL_CAPITAL=100.0
```

### 3. Configurar Portfolios (`config.py`)

Para que la matemática de convicción funcione correctamente, debes asignar el capital activo estimado de cada ballena en `config.py`:

```python
WHALE_PORTFOLIOS: dict[str, float] = {
    "DrPufferfish": 1_200_000.0,
    "swisstony":    2_000_000.0,
}
# Las carteras no especificadas usarán DEFAULT_WHALE_PORTFOLIO_USD ($500.000)
```

---

## 🚀 Uso

Ejecuta el orquestador principal:

```bash
python main_copytrade.py
```

El bot iniciará `PaperTrader` independientes virtuales para cada whale monitorizada.

Pulsa `Ctrl+C` para detener el bot. El estado de cada *PaperTrader* se guardará automáticamente en disco (`data/paper_state_*.json`) y se recuperará en el próximo reinicio.

---

## 📊 Revisión de Resultados

Todas las ejecuciones de copy-trade y liquidaciones se registran en:
- `data/trades.csv` (Registro detallado CSV para Pandas/Excel)
- `data/bot.log` (Log técnico rotativo)

---

**⚠️ Disclaimer**: Software para investigación algorítmica y DeFi. El autor no se hace responsable de las pérdidas de capital reales incurridas en modo "live".
