# Polymarket BTC 5-min Scalping Bot

> **Phase 1 – Paper Trading Only**
> Este bot **no** envía órdenes reales. Lee el orderbook público de Polymarket y simula operaciones con un capital virtual.

---

## Estructura del Proyecto

```
├── main.py                 # Orquestador principal (entry point)
├── config.py               # Parámetros de configuración centralizados
├── requirements.txt        # Dependencias de Python
├── .env_example            # Plantilla de variables de entorno
├── README.md               # Este archivo
├── src/
│   ├── __init__.py
│   ├── polymarket_api.py   # Conexión a la API CLOB + Gamma de Polymarket
│   ├── strategy.py         # Lógica de la estrategia de momentum scalping
│   ├── paper_trader.py     # Gestor de capital virtual y ejecución simulada
│   └── logger.py           # Logging a CSV + consola + archivo
└── data/
    ├── .gitkeep
    ├── trades.csv           # (auto-generado) Log de todas las operaciones
    └── bot.log              # (auto-generado) Log detallado del bot
```

## Estrategia: Momentum Scalping

1. **Espera** – Tras detectar un mercado de 5 min, espera ~30 s para dejar que se forme el precio inicial.
2. **Observación** – Muestrea el mid-price del token YES a intervalos regulares.
3. **Señal de entrada** – Si la probabilidad se mueve ≥ 8% (configurable) hacia un lado:
   - Probabilidad subiendo → **Compra YES**
   - Probabilidad bajando → **Compra NO**
4. **Gestión de posición**:
   - **Take-profit**: cierra cuando el precio se mueve +3 pp a nuestro favor.
   - **Stop-loss**: cierra cuando se mueve -4 pp en contra.
   - **Timeout de seguridad**: cierra tras 180 s (NUNCA mantener hasta resolución).
5. **Regla de oro**: NUNCA mantener la posición hasta que cierre el mercado.

## Instalación

```bash
# 1. Crear entorno virtual (recomendado)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. (Opcional) Copiar la plantilla de entorno
copy .env_example .env
```

## Uso

```bash
# Ejecutar en modo continuo (busca mercados en bucle)
python main.py

# Ejecutar un solo ciclo (un mercado) y salir
python main.py --once
```

Pulsa `Ctrl+C` para detener el bot de forma limpia.

## Configuración

Todos los parámetros se pueden ajustar en `config.py` o mediante variables de entorno en `.env`:

| Parámetro | Default | Descripción |
|---|---|---|
| `INITIAL_CAPITAL` | 50.0 | Capital virtual inicial (€) |
| `WAIT_SECONDS` | 30 | Segundos de espera tras apertura del mercado |
| `MOMENTUM_THRESHOLD` | 0.08 | Cambio mínimo en probabilidad para señal (8 pp) |
| `TAKE_PROFIT` | 0.03 | Distancia de take-profit (3 pp) |
| `STOP_LOSS` | 0.04 | Distancia de stop-loss (4 pp) |
| `POSITION_SIZE_PCT` | 0.20 | Fracción del capital por operación (20%) |
| `MAX_HOLD_SECONDS` | 180 | Tiempo máximo de mantenimiento de posición |

## Revisión de Resultados

Las operaciones se guardan automáticamente en `data/trades.csv`. Se puede analizar con pandas:

```python
import pandas as pd
df = pd.read_csv("data/trades.csv")
print(df[["side", "entry_price", "exit_price", "pnl", "exit_reason"]])
print(f"PnL total: {df['pnl'].sum():.2f} €")
```

## Próximos Pasos (Fase 2)

- [ ] Modelo de slippage basado en la profundidad del orderbook
- [ ] Conexión WebSocket para datos en tiempo real
- [ ] Integración con `py-clob-client` para enviar órdenes reales
- [ ] Panel de control en tiempo real (Streamlit / Grafana)
- [ ] Backtest con datos históricos

---

**⚠️ Disclaimer**: Este software es solo para fines educativos y de investigación. El trading conlleva riesgos significativos. No inviertas dinero que no puedas permitirte perder.
