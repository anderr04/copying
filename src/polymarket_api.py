"""
polymarket_api.py – Interface to Polymarket CLOB & Gamma APIs.

Copy-Trading edition: provides orderbook access and market parsing.
BTC up/down market discovery code has been removed (legacy momentum strategy).
"""

from __future__ import annotations

import math
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class Token:
    """Represents one outcome token (YES or NO) of a Polymarket market."""
    token_id: str
    outcome: str          # "Yes" or "No"
    price: float = 0.0    # last known mid-price (0-1)


@dataclass
class Market:
    """Represents a single Polymarket event / condition."""
    condition_id: str
    question: str
    description: str
    end_date_iso: str
    tokens: list[Token] = field(default_factory=list)
    active: bool = True
    closed: bool = False
    game_start_time: Optional[str] = None  # ISO timestamp
    event_id: Optional[str] = None         # parent event ID
    slug: str = ""                         # event slug


@dataclass
class OrderbookSnapshot:
    """A point-in-time snapshot of the orderbook for one token."""
    token_id: str
    timestamp: float            # time.time()
    best_bid: float = 0.0
    best_ask: float = 1.0
    mid_price: float = 0.5
    spread: float = 1.0
    spread_pct: float = 1.0     # spread as fraction of mid_price
    bids: list[dict] = field(default_factory=list)  # [{price, size}]
    asks: list[dict] = field(default_factory=list)

    # ── Orderbook depth & imbalance helpers ──────────────────────

    def bid_depth(self, levels: int = 3) -> float:
        """Sum of bid sizes in the top *levels* price levels."""
        sorted_bids = sorted(self.bids, key=lambda b: b["price"], reverse=True)
        return sum(float(b["size"]) for b in sorted_bids[:levels])

    def ask_depth(self, levels: int = 3) -> float:
        """Sum of ask sizes in the top *levels* (cheapest) ask levels."""
        sorted_asks = sorted(self.asks, key=lambda a: a["price"])
        return sum(float(a["size"]) for a in sorted_asks[:levels])

    def imbalance(self, levels: int = 3) -> float:
        """
        Orderbook imbalance: bid_depth / ask_depth for top *levels*.
        > 1.0  →  buy pressure (more bids stacked).
        < 1.0  →  sell pressure (more asks stacked).
        Clamped to [0.05, 20.0] to avoid infinities.
        """
        bd = self.bid_depth(levels)
        ad = self.ask_depth(levels)
        if ad <= 0:
            return 20.0
        if bd <= 0:
            return 0.05
        return max(0.05, min(20.0, bd / ad))

    def total_depth(self, levels: int = 3) -> float:
        """Total visible liquidity in top *levels* (bid + ask)."""
        return self.bid_depth(levels) + self.ask_depth(levels)

    def effective_buy_price(self, shares: float) -> float:
        """VWAP for buying *shares* by walking through ask levels."""
        if not self.asks or shares <= 0:
            return self.best_ask
        sorted_asks = sorted(self.asks, key=lambda a: a["price"])
        filled = 0.0
        cost = 0.0
        for level in sorted_asks:
            available = float(level["size"])
            price = float(level["price"])
            fill = min(shares - filled, available)
            cost += fill * price
            filled += fill
            if filled >= shares:
                break
        return cost / filled if filled > 0 else self.best_ask

    def effective_sell_price(self, shares: float) -> float:
        """VWAP for selling *shares* by walking through bid levels."""
        if not self.bids or shares <= 0:
            return self.best_bid
        sorted_bids = sorted(self.bids, key=lambda b: b["price"], reverse=True)
        filled = 0.0
        proceeds = 0.0
        for level in sorted_bids:
            available = float(level["size"])
            price = float(level["price"])
            fill = min(shares - filled, available)
            proceeds += fill * price
            filled += fill
            if filled >= shares:
                break
        return proceeds / filled if filled > 0 else self.best_bid




# ── API Client ───────────────────────────────────────────────────────

class PolymarketClient:
    """Thin wrapper around the Polymarket public REST APIs."""

    def __init__(
        self,
        clob_url: str = config.POLYMARKET_CLOB_URL,
        gamma_url: str = config.POLYMARKET_GAMMA_URL,
    ):
        self.clob_url = clob_url.rstrip("/")
        self.gamma_url = gamma_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "PolyCopyTradeBot/1.0",
        })



    # ── Sorting & De-duplication ─────────────────────────────────

    @staticmethod
    def _sort_by_soonest(markets: list[Market]) -> None:
        """
        Sort markets in-place: soonest FUTURE resolution first.
        Markets whose end_date is already past are pushed to the end.
        """
        now_ts = time.time()

        def _end_key(m: Market) -> tuple[int, float]:
            if not m.end_date_iso:
                return (1, float("inf"))
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    dt = datetime.strptime(m.end_date_iso, fmt).replace(
                        tzinfo=timezone.utc
                    )
                    ts = dt.timestamp()
                    # Future markets first (0), then expired (1)
                    is_expired = 0 if ts > now_ts else 1
                    return (is_expired, ts)
                except ValueError:
                    continue
            return (1, float("inf"))

        markets.sort(key=_end_key)

    @staticmethod
    def _deduplicate(markets: list[Market]) -> list[Market]:
        """Remove duplicate markets by condition_id, keeping first occurrence."""
        seen: set[str] = set()
        unique: list[Market] = []
        for m in markets:
            if m.condition_id not in seen:
                seen.add(m.condition_id)
                unique.append(m)
        return unique

    # ── Market Parsing ───────────────────────────────────────────

    def _parse_market(
        self, raw: dict, event_id: Optional[str] = None
    ) -> Optional[Market]:
        """
        Parse a raw market dict from the Gamma API into our Market dataclass.
        """
        condition_id = raw.get("conditionId", "") or raw.get("condition_id", "")
        if not condition_id:
            return None

        tokens = self._extract_tokens(raw)
        if not tokens:
            return None

        end_date = raw.get("endDate", "") or raw.get("end_date_iso", "")

        return Market(
            condition_id=condition_id,
            question=raw.get("question", ""),
            description=raw.get("description", ""),
            end_date_iso=end_date,
            tokens=tokens,
            active=raw.get("active", True),
            closed=raw.get("closed", False),
            game_start_time=raw.get("game_start_time") or raw.get("startDate"),
            event_id=event_id,
        )

    @staticmethod
    def _extract_tokens(raw: dict) -> list[Token]:
        """
        Extract Token objects from a market dict.

        Handles two formats:
        1. A 'tokens' array with objects {token_id, outcome, price}
        2. Separate 'clobTokenIds' (JSON string) + 'outcomes' (JSON string)
           + 'outcomePrices' (JSON string)
        """
        import json as _json

        # Format 1: explicit tokens array
        raw_tokens = raw.get("tokens", [])
        if raw_tokens and isinstance(raw_tokens, list) and isinstance(raw_tokens[0], dict):
            return [
                Token(
                    token_id=str(t.get("token_id", "")),
                    outcome=t.get("outcome", ""),
                    price=float(t.get("price", 0.5)),
                )
                for t in raw_tokens
                if t.get("token_id")
            ]

        # Format 2: clobTokenIds + outcomes + outcomePrices as JSON strings
        clob_ids_raw = raw.get("clobTokenIds", "[]")
        outcomes_raw = raw.get("outcomes", "[]")
        prices_raw = raw.get("outcomePrices", "[]")

        try:
            clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
            outcomes = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = _json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        except (_json.JSONDecodeError, TypeError):
            return []

        if not clob_ids or not outcomes:
            return []

        tokens: list[Token] = []
        for i, token_id in enumerate(clob_ids):
            outcome = outcomes[i] if i < len(outcomes) else f"Outcome{i}"
            price = float(prices[i]) if i < len(prices) else 0.5
            tokens.append(Token(
                token_id=str(token_id),
                outcome=outcome,
                price=price,
            ))

        return tokens

    # ── Orderbook ────────────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> OrderbookSnapshot:
        """
        Fetch the current orderbook for a given token_id from the CLOB API.
        """
        snap = OrderbookSnapshot(token_id=token_id, timestamp=time.time())

        try:
            resp = self.session.get(
                f"{self.clob_url}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            raw_bids = data.get("bids", [])
            raw_asks = data.get("asks", [])

            snap.bids = [
                {"price": float(b.get("price", 0)), "size": float(b.get("size", 0))}
                for b in raw_bids
            ]
            snap.asks = [
                {"price": float(a.get("price", 0)), "size": float(a.get("size", 0))}
                for a in raw_asks
            ]

            if snap.bids:
                snap.best_bid = max(b["price"] for b in snap.bids)
            else:
                snap.best_bid = 0.0

            if snap.asks:
                snap.best_ask = min(a["price"] for a in snap.asks)
            else:
                snap.best_ask = 1.0

            snap.spread = snap.best_ask - snap.best_bid
            snap.mid_price = (snap.best_bid + snap.best_ask) / 2.0
            snap.spread_pct = (snap.spread / snap.mid_price) if snap.mid_price > 0 else 1.0

        except requests.RequestException as exc:
            logger.error("Failed to fetch orderbook for %s: %s", token_id, exc)

        return snap

    def get_mid_prices(self, market: Market) -> dict[str, float]:
        """
        Convenience: return a dict  {outcome: mid_price}  for every token
        in the market, e.g. {"Yes": 0.62, "No": 0.38}.
        """
        prices: dict[str, float] = {}
        for token in market.tokens:
            snap = self.get_orderbook(token.token_id)
            prices[token.outcome] = snap.mid_price
            token.price = snap.mid_price
        return prices

    # ── Price Stream (simple polling) ────────────────────────────

    def poll_price(self, token_id: str) -> float:
        """
        Quick single-call: return the mid-price of the given token.
        """
        snap = self.get_orderbook(token_id)
        return snap.mid_price
    def poll_orderbook(self, token_id: str) -> OrderbookSnapshot:
        """
        Return the full OrderbookSnapshot (bid, ask, spread, etc.).
        Used by the strategy for realistic entry/exit pricing.
        """
        return self.get_orderbook(token_id)