"""
token_registry.py – Mapeo de token_id on-chain ↔ datos del mercado CLOB.

Cuando el listener detecta un OrderFilled, el evento contiene un uint256
(el conditional token ID).  Este módulo traduce ese ID a información útil:
    - condition_id (ID del mercado en Polymarket)
    - outcome      ("Yes" / "No")
    - question     (título legible del mercado)
    - event_slug   (slug del evento)

Se construye consultando la Gamma API de Polymarket (mismo endpoint que
usa el PolymarketClient existente), y se refresca periódicamente.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional
import json as _json

import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    """Información de un mercado asociada a un token_id."""
    token_id: str           # decimal string del uint256
    condition_id: str       # condition ID del mercado
    question: str           # pregunta / título del mercado
    outcome: str            # "Yes" o "No"
    event_slug: str = ""    # slug del evento padre
    end_date_iso: str = ""  # fecha de cierre del mercado


class TokenRegistry:
    """
    Cache de resolución token_id → MarketInfo.

    Pre-carga mercados activos desde la Gamma API al inicio,
    y refresca periódicamente.  Para token_ids desconocidos,
    intenta una búsqueda bajo demanda.
    """

    def __init__(
        self,
        gamma_url: str = config.POLYMARKET_GAMMA_URL,
        refresh_interval: float = config.TOKEN_REGISTRY_REFRESH_S,
        preload_limit: int = config.TOKEN_REGISTRY_PRELOAD_LIMIT,
    ):
        self.gamma_url = gamma_url.rstrip("/")
        self.refresh_interval = refresh_interval
        self.preload_limit = preload_limit

        self._cache: dict[str, MarketInfo] = {}   # token_id → MarketInfo
        self._last_refresh: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "PolyWhaleTracker/1.0",
        })

    # ── Public API ───────────────────────────────────────────────

    def preload(self) -> int:
        """
        Carga inicial de mercados activos.
        Retorna el número de tokens registrados.
        """
        count = self._fetch_active_markets()
        self._last_refresh = time.time()
        logger.info(
            "📚 Token Registry: %d tokens cargados de %d mercados.",
            count, len(set(m.condition_id for m in self._cache.values())),
        )
        return count

    def refresh_if_stale(self) -> None:
        """Refresca el cache si ha pasado el intervalo configurado."""
        if time.time() - self._last_refresh > self.refresh_interval:
            logger.debug("Refrescando token registry …")
            self._fetch_active_markets()
            self._last_refresh = time.time()

    def lookup(self, token_id: str | int) -> Optional[MarketInfo]:
        """
        Busca un token_id en el cache.
        Si no lo encuentra, intenta una búsqueda bajo demanda.
        """
        key = str(token_id)
        info = self._cache.get(key)
        if info is not None:
            return info

        # Intento de resolución bajo demanda
        info = self._search_token_by_id(key)
        if info is not None:
            self._cache[key] = info
            logger.info(
                "🔍 Token resuelto bajo demanda: %s → '%s' [%s]",
                key[:20] + "…", info.question[:50], info.outcome,
            )
        return info

    def has(self, token_id: str | int) -> bool:
        """Comprueba si el token está en el cache (sin búsqueda activa)."""
        return str(token_id) in self._cache

    @property
    def size(self) -> int:
        return len(self._cache)

    # ── Carga desde Gamma API ────────────────────────────────────

    def _fetch_active_markets(self) -> int:
        """
        Consulta la Gamma API para obtener mercados activos
        y registrar todos sus token_ids.
        """
        total_tokens = 0
        try:
            resp = self._session.get(
                f"{self.gamma_url}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": self.preload_limit,
                },
                timeout=20,
            )
            resp.raise_for_status()
            events = resp.json()

            if not isinstance(events, list):
                logger.warning("Respuesta inesperada de Gamma API (no es lista).")
                return 0

            for event in events:
                event_slug = event.get("slug", "")
                for raw_mkt in event.get("markets", []):
                    condition_id = (
                        raw_mkt.get("conditionId", "")
                        or raw_mkt.get("condition_id", "")
                    )
                    if not condition_id:
                        continue

                    question = raw_mkt.get("question", "")
                    end_date = raw_mkt.get("endDate", "") or raw_mkt.get("end_date_iso", "")
                    tokens = self._extract_tokens(raw_mkt)

                    for tok_id, outcome in tokens:
                        info = MarketInfo(
                            token_id=tok_id,
                            condition_id=condition_id,
                            question=question,
                            outcome=outcome,
                            event_slug=event_slug,
                            end_date_iso=end_date,
                        )
                        self._cache[tok_id] = info
                        total_tokens += 1

        except requests.RequestException as exc:
            logger.error("Error cargando mercados de Gamma API: %s", exc)

        return total_tokens

    def _search_token_by_id(self, token_id: str) -> Optional[MarketInfo]:
        """
        Búsqueda bajo demanda: consulta la Gamma API directamente con el
        token_id.  Usa /markets?clob_token_ids=... que encuentra mercados
        activos Y cerrados (deportes, etc.).
        """

        # ── 1) Búsqueda directa por clob_token_ids (más fiable) ─────────
        try:
            resp = self._session.get(
                f"{self.gamma_url}/markets",
                params={"clob_token_ids": token_id},
                timeout=15,
            )
            resp.raise_for_status()
            markets = resp.json()

            if isinstance(markets, list) and markets:
                raw_mkt = markets[0]
                condition_id = (
                    raw_mkt.get("conditionId", "")
                    or raw_mkt.get("condition_id", "")
                )
                question = raw_mkt.get("question", "")
                end_date = raw_mkt.get("endDate", "") or raw_mkt.get("end_date_iso", "")
                event_slug = raw_mkt.get("slug", "") or raw_mkt.get("event_slug", "")

                # Extraer tokens y cachearlos todos
                tokens = self._extract_tokens(raw_mkt)
                found: Optional[MarketInfo] = None
                for tok_id, outcome in tokens:
                    info = MarketInfo(
                        token_id=tok_id,
                        condition_id=condition_id,
                        question=question,
                        outcome=outcome,
                        event_slug=event_slug,
                        end_date_iso=end_date,
                    )
                    self._cache[tok_id] = info
                    if tok_id == token_id:
                        found = info

                # Si no se extrajo del par de tokens, inferir outcome
                if found is None:
                    outcomes_raw = raw_mkt.get("outcomes", "[]")
                    clob_ids_raw = raw_mkt.get("clobTokenIds", "[]")
                    try:
                        clob_ids = (
                            _json.loads(clob_ids_raw)
                            if isinstance(clob_ids_raw, str)
                            else clob_ids_raw
                        ) or []
                        outcomes_list = (
                            _json.loads(outcomes_raw)
                            if isinstance(outcomes_raw, str)
                            else outcomes_raw
                        ) or []
                    except (_json.JSONDecodeError, TypeError):
                        clob_ids = []
                        outcomes_list = []

                    outcome = "?"
                    for i, tid in enumerate(clob_ids):
                        if str(tid) == token_id and i < len(outcomes_list):
                            outcome = outcomes_list[i]
                            break

                    found = MarketInfo(
                        token_id=token_id,
                        condition_id=condition_id,
                        question=question,
                        outcome=outcome,
                        event_slug=event_slug,
                        end_date_iso=end_date,
                    )
                    self._cache[token_id] = found

                return found

        except requests.RequestException as exc:
            logger.warning("Error en búsqueda directa por token_id: %s", exc)

        # ── 2) Fallback: barrido amplio (incluyendo cerrados) ────────────
        try:
            resp = self._session.get(
                f"{self.gamma_url}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 500,
                },
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json()

            if not isinstance(events, list):
                return None

            for event in events:
                event_slug = event.get("slug", "")
                for raw_mkt in event.get("markets", []):
                    condition_id = (
                        raw_mkt.get("conditionId", "")
                        or raw_mkt.get("condition_id", "")
                    )
                    if not condition_id:
                        continue

                    tokens = self._extract_tokens(raw_mkt)
                    for tok_id, outcome in tokens:
                        if tok_id not in self._cache:
                            self._cache[tok_id] = MarketInfo(
                                token_id=tok_id,
                                condition_id=condition_id,
                                question=raw_mkt.get("question", ""),
                                outcome=outcome,
                                event_slug=event_slug,
                                end_date_iso=raw_mkt.get("endDate", ""),
                            )
                        if tok_id == token_id:
                            return self._cache[tok_id]

        except requests.RequestException as exc:
            logger.warning("Error en búsqueda bajo demanda (fallback): %s", exc)

        return None

    # ── Token extraction (reutiliza lógica del PolymarketClient) ─

    # ── Market Resolution Check ────────────────────────────────

    def check_resolution(self, token_id: str) -> dict | None:
        """
        Query the Gamma API to check if a market is resolved.

        Returns a dict with:
            - closed: bool
            - resolution_price: float (1.0 if our outcome won, 0.0 if lost)
            - winning_outcome: str (name of the winning outcome)
        Or None if the query fails.
        """
        try:
            resp = self._session.get(
                f"{self.gamma_url}/markets",
                params={"clob_token_ids": token_id},
                timeout=15,
            )
            resp.raise_for_status()
            markets = resp.json()

            if not isinstance(markets, list) or not markets:
                return None

            raw_mkt = markets[0]
            closed = raw_mkt.get("closed", False)

            if not closed:
                return {"closed": False, "resolution_price": None, "winning_outcome": None}

            # Parse outcome prices and token IDs
            outcome_prices_raw = raw_mkt.get("outcomePrices", "[]")
            outcomes_raw = raw_mkt.get("outcomes", "[]")
            clob_ids_raw = raw_mkt.get("clobTokenIds", "[]")

            try:
                outcome_prices = (
                    _json.loads(outcome_prices_raw)
                    if isinstance(outcome_prices_raw, str)
                    else outcome_prices_raw
                ) or []
                outcomes = (
                    _json.loads(outcomes_raw)
                    if isinstance(outcomes_raw, str)
                    else outcomes_raw
                ) or []
                clob_ids = (
                    _json.loads(clob_ids_raw)
                    if isinstance(clob_ids_raw, str)
                    else clob_ids_raw
                ) or []
            except (_json.JSONDecodeError, TypeError):
                logger.warning("Error parsing resolution data for token %s…", token_id[:20])
                return None

            # Find our token's index and resolution price
            resolution_price = None
            for i, tid in enumerate(clob_ids):
                if str(tid) == token_id and i < len(outcome_prices):
                    try:
                        resolution_price = float(outcome_prices[i])
                    except (ValueError, TypeError):
                        resolution_price = None
                    break

            # Find the winning outcome (price == 1.0)
            winning_outcome = None
            for i, price_str in enumerate(outcome_prices):
                try:
                    if float(price_str) >= 0.99 and i < len(outcomes):
                        winning_outcome = outcomes[i]
                        break
                except (ValueError, TypeError):
                    continue

            return {
                "closed": True,
                "resolution_price": resolution_price,
                "winning_outcome": winning_outcome,
            }

        except requests.RequestException as exc:
            logger.warning("Error checking resolution for token %s…: %s", token_id[:20], exc)
            return None

    @staticmethod
    def _extract_tokens(raw_mkt: dict) -> list[tuple[str, str]]:
        """
        Extrae pares (token_id, outcome) de un dict de mercado.
        Soporta los dos formatos de la Gamma API.
        """
        results: list[tuple[str, str]] = []

        # Formato 1: lista de tokens directa
        raw_tokens = raw_mkt.get("tokens", [])
        if raw_tokens and isinstance(raw_tokens, list) and isinstance(raw_tokens[0], dict):
            for t in raw_tokens:
                tid = str(t.get("token_id", ""))
                outcome = t.get("outcome", "")
                if tid:
                    results.append((tid, outcome))
            return results

        # Formato 2: campos JSON-encoded separados
        clob_ids_raw = raw_mkt.get("clobTokenIds", "[]")
        outcomes_raw = raw_mkt.get("outcomes", "[]")

        try:
            clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        except (_json.JSONDecodeError, TypeError):
            return results

        if not clob_ids or not outcomes:
            return results

        for i, tok_id in enumerate(clob_ids):
            outcome = outcomes[i] if i < len(outcomes) else f"Outcome{i}"
            results.append((str(tok_id), outcome))

        return results
