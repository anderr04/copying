"""
polymarket_api.py – Interface to Polymarket CLOB & Gamma APIs.

v7 – Multi-Timeframe market discovery
--------------------------------------
Supports slug-based discovery for:
    daily  → bitcoin-up-or-down-on-{month}-{day}
    4h     → btc-updown-4h-{ts}  (14 400 s windows, offset 3 600)
    15m    → btc-updown-15m-{ts} (900 s windows)
    5m     → btc-updown-5m-{ts}  (300 s windows)

Public, unauthenticated endpoints only.
"""

from __future__ import annotations

import math
import time
import logging
import re
import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
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


# ── Slug helpers (multi-timeframe) ───────────────────────────────────

_TIMEFRAME_META: dict[str, dict] = {
    "5m":    {"prefix": "btc-updown-5m-",  "window": 300,   "offset": 0},
    "15m":   {"prefix": "btc-updown-15m-", "window": 900,   "offset": 0},
    "4h":    {"prefix": "btc-updown-4h-",  "window": 14400, "offset": 3600},
    # daily uses date-based slugs, handled separately
}

_MONTH_NAMES = {
    1: "january", 2: "february", 3: "march", 4: "april",
    5: "may", 6: "june", 7: "july", 8: "august",
    9: "september", 10: "october", 11: "november", 12: "december",
}


def _window_start(timeframe: str) -> int:
    """Return the UNIX timestamp of the current window start for *timeframe*."""
    meta = _TIMEFRAME_META.get(timeframe)
    if meta is None:
        raise ValueError(f"No timestamp-based slug for timeframe '{timeframe}'")
    now = int(time.time())
    offset = meta["offset"]
    window = meta["window"]
    return ((now - offset) // window) * window + offset


def _candidate_slugs_ts(
    timeframe: str,
    look_ahead: int = 3,
    look_behind: int = 1,
) -> list[str]:
    """
    Generate candidate slugs for timestamp-based timeframes (5m, 15m, 4h).
    """
    meta = _TIMEFRAME_META[timeframe]
    prefix = meta["prefix"]
    window = meta["window"]
    base = _window_start(timeframe)
    slugs: list[str] = [f"{prefix}{base}"]
    for i in range(1, max(look_ahead, look_behind) + 1):
        if i <= look_ahead:
            slugs.append(f"{prefix}{base + i * window}")
        if i <= look_behind:
            slugs.append(f"{prefix}{base - i * window}")
    return slugs


def _candidate_slugs_daily(look_ahead: int = 1, look_behind: int = 1) -> list[str]:
    """
    Generate candidate slugs for DAILY markets.
    Pattern: bitcoin-up-or-down-on-{month}-{day}
    """
    now_utc = datetime.now(timezone.utc)
    slugs: list[str] = []
    for delta in range(- look_behind, look_ahead + 1):
        d = now_utc + timedelta(days=delta)
        month = _MONTH_NAMES[d.month]
        day = d.day
        slugs.append(f"bitcoin-up-or-down-on-{month}-{day}")
    return slugs


def candidate_slugs(timeframe: str | None = None) -> list[str]:
    """
    Public helper: generate candidate slugs for the given (or active) timeframe.
    """
    tf = (timeframe or config.TIMEFRAME).lower()
    if tf == "daily":
        return _candidate_slugs_daily()
    return _candidate_slugs_ts(tf)


# ── Flexible regex patterns (EN + ES) ───────────────────────────────
# Matches both English and Spanish titles:
#   EN: "Bitcoin Up or Down - February 23, 6:45AM-6:50AM ET"
#   ES: "Bitcoin arriba o abajo - 5 Minutos"
_BTC_PATTERN = re.compile(
    r"(btc|bitcoin)",
    re.IGNORECASE,
)

_UPDOWN_PATTERN = re.compile(
    r"(up\s*(or|/)\s*down|arriba\s*(o|/)\s*abajo|sube\s*(o|/)\s*baja)",
    re.IGNORECASE,
)

_5MIN_PATTERN = re.compile(
    r"(5[- ]?min|5[- ]?minute|5[- ]?minuto|5m\b|\d{1,2}:\d{2}\s*(AM|PM)\s*[-–]\s*\d{1,2}:\d{2}\s*(AM|PM))",
    re.IGNORECASE,
)

_SLUG_5M_PATTERN = re.compile(r"btc-updown-5m-\d+", re.IGNORECASE)
_SLUG_15M_PATTERN = re.compile(r"btc-updown-15m-\d+", re.IGNORECASE)
_SLUG_4H_PATTERN = re.compile(r"btc-updown-4h-\d+", re.IGNORECASE)
_SLUG_DAILY_PATTERN = re.compile(
    r"bitcoin-up-or-down-on-[a-z]+-\d+", re.IGNORECASE,
)

# Combined: matches ANY BTC up/down slug
_SLUG_ANY_PATTERN = re.compile(
    r"(btc-updown-(5m|15m|4h)-\d+|bitcoin-up-or-down-on-[a-z]+-\d+)",
    re.IGNORECASE,
)

# Timeframe-specific title patterns
_15MIN_PATTERN = re.compile(r"(15[- ]?min|15m\b)", re.IGNORECASE)
_4H_PATTERN = re.compile(r"(4[- ]?hour|4h\b|4[- ]?hora)", re.IGNORECASE)
_DAILY_PATTERN = re.compile(
    r"(on\s+(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+\d{1,2})",
    re.IGNORECASE,
)

_SLUG_PATTERN_BY_TF: dict[str, re.Pattern] = {
    "5m": _SLUG_5M_PATTERN,
    "15m": _SLUG_15M_PATTERN,
    "4h": _SLUG_4H_PATTERN,
    "daily": _SLUG_DAILY_PATTERN,
}


def _is_btc_market_for_timeframe(
    text: str, slug: str, timeframe: str,
) -> bool:
    """
    Return True if text/slug matches a BTC up/down market for *timeframe*.
    """
    # Slug match is most reliable
    slug_pat = _SLUG_PATTERN_BY_TF.get(timeframe)
    if slug_pat and slug and slug_pat.search(slug):
        return True

    text_lower = text.lower()
    has_btc = bool(_BTC_PATTERN.search(text_lower))
    has_updown = bool(_UPDOWN_PATTERN.search(text_lower))

    if not (has_btc and has_updown):
        return False

    if timeframe == "daily":
        return bool(_DAILY_PATTERN.search(text_lower))
    if timeframe == "4h":
        return bool(_4H_PATTERN.search(text_lower))
    if timeframe == "15m":
        return bool(_15MIN_PATTERN.search(text_lower))
    if timeframe == "5m":
        return bool(_5MIN_PATTERN.search(text_lower))

    return False


def _is_btc_5min_market(text: str, slug: str = "") -> bool:
    """Legacy: check if text matches a 5-min BTC market."""
    return _is_btc_market_for_timeframe(text, slug, "5m")


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
            "User-Agent": "PolyBTCScalperBot/0.1",
        })

    # ── Market Discovery ─────────────────────────────────────────

    def search_active_btc_markets(
        self, timeframe: str | None = None,
    ) -> list[Market]:
        """
        Find active BTC up/down markets for the given *timeframe*.
        Falls back to config.TIMEFRAME if not specified.

        Strategy:
        1. SLUG LOOKUP – direct /events?slug=<slug> for candidate windows.
        2. QUERY + FILTER – /events?query=Bitcoin Up or Down, filter by
           slug/title patterns.
        """
        tf = (timeframe or config.TIMEFRAME).lower()
        slugs = candidate_slugs(tf)

        logger.info(
            "🔎 Buscando mercados BTC [%s] — %d slugs candidatos …",
            tf.upper(), len(slugs),
        )
        for s in slugs[:4]:
            logger.debug("  → slug: %s", s)

        # ── Strategy 1: direct slug lookup ───────────────────
        markets = self._search_by_slug_multi(slugs, tf)
        if markets:
            self._sort_by_soonest(markets)
            return markets

        # ── Strategy 2: query + regex filter ─────────────────
        markets = self._search_by_query(tf)
        if markets:
            self._sort_by_soonest(markets)
            return markets

        logger.debug("No BTC %s markets found.", tf)
        return []

    # Legacy alias so old code keeps working
    def search_active_btc_5min_markets(self) -> list[Market]:
        return self.search_active_btc_markets("5m")

    # ── Slug-based search (all timeframes) ───────────────────────

    def _search_by_slug_multi(
        self, slugs: list[str], timeframe: str,
    ) -> list[Market]:
        """Try exact slug lookups for each candidate slug."""
        markets: list[Market] = []

        for slug in slugs:
            try:
                resp = self.session.get(
                    f"{self.gamma_url}/events",
                    params={"slug": slug},
                    timeout=10,
                )
                resp.raise_for_status()
                events = resp.json()

                if not isinstance(events, list) or not events:
                    continue

                for event in events:
                    event_id = str(event.get("id", ""))
                    event_slug = event.get("slug", "")

                    for raw_mkt in event.get("markets", []):
                        if raw_mkt.get("closed", False) or not raw_mkt.get("active", False):
                            continue
                        mkt = self._parse_market(raw_mkt, event_id=event_id)
                        if mkt:
                            mkt.slug = event_slug
                            markets.append(mkt)

                if events:
                    title = events[0].get("title", "?")
                    logger.info(
                        "🎯 Slug '%s' → '%s'  (%d active)",
                        slug, title,
                        sum(1 for m in markets if m.slug == events[0].get("slug", "")),
                    )

            except requests.RequestException as exc:
                logger.debug("Slug lookup failed '%s': %s", slug, exc)

        return self._deduplicate(markets)

    # ── Query-based search with timeframe filter ─────────────────

    def _search_by_query(self, timeframe: str) -> list[Market]:
        """Search with text queries, then filter by timeframe slug/title."""
        markets: list[Market] = []
        queries = getattr(config, "MARKET_SEARCH_QUERIES", [
            "Bitcoin Up or Down",
        ])

        for query in queries:
            try:
                resp = self.session.get(
                    f"{self.gamma_url}/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 50,
                        "query": query,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                events = resp.json()
                if not isinstance(events, list):
                    continue

                for event in events:
                    event_slug = event.get("slug", "")
                    event_id = str(event.get("id", ""))
                    event_title = event.get("title", "")

                    combined = event_title
                    if not _is_btc_market_for_timeframe(combined, event_slug, timeframe):
                        continue

                    for raw_mkt in event.get("markets", []):
                        if raw_mkt.get("closed", False) or not raw_mkt.get("active", False):
                            continue
                        mkt = self._parse_market(raw_mkt, event_id=event_id)
                        if mkt:
                            mkt.slug = event_slug
                            markets.append(mkt)

                logger.info(
                    "Query='%s' [%s] → %d events, %d matching.",
                    query, timeframe, len(events), len(markets),
                )
                if markets:
                    break

            except requests.RequestException as exc:
                logger.warning("Query search failed '%s': %s", query, exc)

        return self._deduplicate(markets)

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