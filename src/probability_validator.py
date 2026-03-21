"""
probability_validator.py – Shadow-mode IA probability validator.

Runs as a BACKGROUND THREAD. Receives trade signals from
CopyTradeEngine via a queue, fetches real-world data, queries Ollama
(local LLM), and logs everything to shadow_trades.csv.

NEVER blocks the main bot loop. If the queue is full or Ollama is
slow, signals are silently dropped.

Architecture:
    CopyTradeEngine ──queue.put_nowait()──▶ ProbabilityValidator
                        (fire & forget)        (background thread)
                                               ├─ CategoryDetector
                                               ├─ DataFetcher (APIs)
                                               └─ Ollama (LLM)
                                               → shadow_trades.csv
"""

from __future__ import annotations

import csv
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

import config

logger = logging.getLogger(__name__)

# ── Shadow CSV ───────────────────────────────────────────────────────
SHADOW_CSV = config.DATA_DIR / "shadow_trades.csv"
SHADOW_COLUMNS = [
    "timestamp",
    "whale_label",
    "market_question",
    "outcome",
    "category",
    "poly_price",
    "whale_price",
    "whale_usd",
    "conviction_pct",
    "our_size_usd",
    "model_probability",
    "model_confidence",
    "model_explanation",
    "model_sources",
    "would_copy",
    "edge_ratio",
    "bot_action",          # what the bot actually did (COPIED / SKIPPED_*)
    "actual_outcome",      # filled later by analysis.py
    "pnl_simulated",       # filled later by analysis.py
    "model_name",
    "latency_ms",
]


# =====================================================================
#  Category Detector
# =====================================================================

class CategoryDetector:
    """Keyword-based market category classifier. Fast, no LLM needed."""

    # (pattern, category) — checked in order, first match wins
    _RULES: list[tuple[str, str]] = [
        # Crypto
        (r"\b(bitcoin|btc|ethereum|eth|solana|sol|crypto|coin|token|defi"
         r"|blockchain|nft|binance|coinbase)\b", "crypto"),
        # NBA
        (r"\b(nba|lakers|celtics|warriors|bucks|76ers|heat|nuggets"
         r"|knicks|bulls|suns|clippers|thunder|cavaliers|rockets"
         r"|timberwolves|pelicans|hawks|hornets|grizzlies|spurs"
         r"|pacers|magic|wizards|pistons|blazers|kings|nets|raptors"
         r"|mavericks|jazz)\b", "sports_nba"),
        # NFL
        (r"\b(nfl|super.?bowl|chiefs|eagles|bills|49ers|cowboys"
         r"|ravens|lions|dolphins|jets|packers|bengals|seahawks"
         r"|steelers|rams|chargers|broncos|saints|falcons|panthers"
         r"|buccaneers|vikings|colts|texans|bears|commanders"
         r"|cardinals|titans|jaguars|patriots|browns|raiders"
         r"|giants)\b", "sports_nfl"),
        # Football / Soccer
        (r"\b(fc |cf |real.madrid|barcelona|manchester|liverpool|chelsea"
         r"|arsenal|tottenham|juventus|milan|inter|napoli|roma"
         r"|bayern|dortmund|psg|lyon|marseille|benfica|porto"
         r"|ajax|feyenoord|la.liga|premier.league|serie.a|bundesliga"
         r"|ligue.1|champions.league|europa.league|world.cup"
         r"|concacaf|conmebol|mls |liga.mx|copa.libertadores"
         r"|spread:|o/u |over.under|goal|assist"
         r"|fiorentina|aston.villa|crystal.palace|celtic|rangers"
         r"|shakhtar|sparta|lech|vitória|grêmio|chapecoense"
         r"|flamengo|palmeiras|corinthians|botafogo|atlético"
         r"|sevilla|villarreal|betis|celta|athletic|sociedad"
         r"|olympique|lille|monaco|nice|rennes|lens|strasbourg"
         r"|midtjylland|freiburg|leverkusen|wolfsburg|schalke"
         r"|gladbach|stuttgart|win on 20[0-9][0-9]-"
         r"|miami.open|laver.cup|atp |wta )\b", "sports_football"),
        # March Madness / College Basketball
        (r"\b(ncaa|march.madness|final.four|sweet.sixteen|elite.eight"
         r"|razorbacks|spartans|tar.heels|blue.devils|bulldogs"
         r"|wildcats|longhorns|cougars|cardinals|fighting.illini"
         r"|boilermakers|jayhawks|mountaineers|volunteers|tigers"
         r"|crimson.tide|seminoles|hurricanes|cavaliers|hokies"
         r"|wolfpack|terrapins|badgers|hawkeyes|cyclones"
         r"|rainbow.warriors|billikens|vandals|lancers|bison"
         r"|saints|gaels|panthers|cowboys|commodores|rams"
         r"|red.storm)\b", "sports_ncaa"),
        # NHL
        (r"\b(nhl|stanley.cup|lightning|thunder|wild|bruins"
         r"|maple.leafs|penguins|capitals|red.wings|blackhawks"
         r"|oilers|flames|canucks|avalanche|blues|predators"
         r"|hurricanes|panthers|senators|canadiens|sabres"
         r"|kraken|islanders|devils|flyers|rangers|ducks"
         r"|sharks|kings|coyotes|jets|blue.jackets)\b", "sports_nhl"),
        # Tennis
        (r"\b(tennis|grand.slam|wimbledon|us.open|french.open"
         r"|australian.open|miami.open|roland.garros|atp|wta"
         r"|djokovic|nadal|federer|alcaraz|sinner|medvedev"
         r"|swiatek|sabalenka|gauff|rybakina|linette)\b", "sports_tennis"),
        # Politics
        (r"\b(trump|biden|election|president|congress|senate"
         r"|democrat|republican|gop|poll|vote|governor|mayor"
         r"|primary|debate|impeach|legislation|executive.order"
         r"|supreme.court|cabinet|vance|harris|desantis|newsom"
         r"|netanyahu|xi.jinping|putin|zelensky|macron|modi"
         r"|ayatollah|khamenei|nato|iran|tariff|fed.rate"
         r"|rate.cut|interest.rate|ipo|stripe)\b", "politics"),
        # Weather
        (r"\b(temperature|weather|forecast|hurricane|tornado|storm"
         r"|celsius|fahrenheit|rainfall|snowfall|drought|flood"
         r"|heat.wave|cold.wave|el.niño|la.niña|climate)\b", "weather"),
        # Economy / Finance (not crypto)
        (r"\b(gdp|inflation|cpi|ppi|unemployment|jobs.report"
         r"|non.?farm|fomc|fed |treasury|yield|bond|recession"
         r"|s&p|nasdaq|dow|crude.oil|gold.price|silver|wti"
         r"|brent|opec)\b", "economy"),
    ]

    _compiled = [(re.compile(pat, re.IGNORECASE), cat) for pat, cat in _RULES]

    @classmethod
    def detect(cls, market_question: str, outcome: str = "") -> str:
        """Classify a market question into a category."""
        text = f"{market_question} {outcome}".lower()
        for pattern, category in cls._compiled:
            if pattern.search(text):
                return category
        return "other"


# =====================================================================
#  Data Fetcher — category-specific real-world data
# =====================================================================

class DataFetcher:
    """Fetches real-world context data based on market category."""

    _TIMEOUT = 5  # seconds per API call

    @classmethod
    def fetch(cls, category: str, market_question: str,
              outcome: str = "") -> dict[str, Any]:
        """Dispatch to the right fetcher. Returns context dict."""
        try:
            if category == "crypto":
                return cls._fetch_crypto(market_question)
            elif category.startswith("sports_"):
                return cls._fetch_sports(market_question, category)
            elif category == "politics":
                return cls._fetch_politics(market_question)
            elif category == "weather":
                return cls._fetch_weather(market_question)
            elif category == "economy":
                return cls._fetch_economy(market_question)
            else:
                return cls._fetch_general(market_question)
        except Exception as e:
            logger.debug("DataFetcher error for '%s': %s", category, e)
            return {"error": str(e), "note": "Context unavailable"}

    # ── Crypto ────────────────────────────────────────────────────

    @classmethod
    def _fetch_crypto(cls, question: str) -> dict:
        """CoinGecko free API — prices, trends."""
        # Extract coin name from question
        coins_map = {
            "bitcoin": "bitcoin", "btc": "bitcoin",
            "ethereum": "ethereum", "eth": "ethereum",
            "solana": "solana", "sol": "solana",
            "cardano": "cardano", "ada": "cardano",
            "dogecoin": "dogecoin", "doge": "dogecoin",
            "xrp": "ripple", "ripple": "ripple",
            "bnb": "binancecoin", "polkadot": "polkadot",
            "avalanche": "avalanche-2", "matic": "matic-network",
        }
        coin_id = "bitcoin"  # default
        q_lower = question.lower()
        for keyword, cg_id in coins_map.items():
            if keyword in q_lower:
                coin_id = cg_id
                break

        url = (
            f"https://api.coingecko.com/api/v3/coins/{coin_id}"
            f"?localization=false&tickers=false&community_data=false"
            f"&developer_data=false"
        )
        r = requests.get(url, timeout=cls._TIMEOUT)
        if r.status_code != 200:
            return {"error": f"CoinGecko {r.status_code}"}

        data = r.json()
        md = data.get("market_data", {})
        return {
            "source": "CoinGecko",
            "coin": data.get("name", coin_id),
            "current_price_usd": md.get("current_price", {}).get("usd"),
            "price_change_24h_pct": md.get(
                "price_change_percentage_24h"),
            "price_change_7d_pct": md.get(
                "price_change_percentage_7d"),
            "price_change_30d_pct": md.get(
                "price_change_percentage_30d"),
            "ath_usd": md.get("ath", {}).get("usd"),
            "market_cap_usd": md.get("market_cap", {}).get("usd"),
            "total_volume_24h": md.get("total_volume", {}).get("usd"),
        }

    # ── Sports ────────────────────────────────────────────────────

    @classmethod
    def _fetch_sports(cls, question: str, category: str) -> dict:
        """TheSportsDB free API — team info, recent results."""
        base = "https://www.thesportsdb.com/api/v1/json/3"

        # Try to extract team names from question
        # e.g. "Will the Lakers win?" → search "Lakers"
        teams = cls._extract_team_names(question)
        results = {}

        for team_name in teams[:2]:  # max 2 teams
            try:
                r = requests.get(
                    f"{base}/searchteams.php",
                    params={"t": team_name},
                    timeout=cls._TIMEOUT,
                )
                if r.status_code == 200:
                    data = r.json()
                    team_list = data.get("teams")
                    if team_list:
                        t = team_list[0]
                        results[team_name] = {
                            "team": t.get("strTeam"),
                            "league": t.get("strLeague"),
                            "stadium": t.get("strStadium"),
                            "description": (t.get("strDescriptionEN")
                                            or "")[:200],
                        }

                        # Get last 5 events
                        team_id = t.get("idTeam")
                        if team_id:
                            r2 = requests.get(
                                f"{base}/eventslast.php",
                                params={"id": team_id},
                                timeout=cls._TIMEOUT,
                            )
                            if r2.status_code == 200:
                                events = (r2.json()
                                          .get("results") or [])
                                recent = []
                                for ev in events[:5]:
                                    recent.append({
                                        "date": ev.get("dateEvent"),
                                        "home": ev.get("strHomeTeam"),
                                        "away": ev.get("strAwayTeam"),
                                        "score": (
                                            f"{ev.get('intHomeScore', '?')}"
                                            f"-"
                                            f"{ev.get('intAwayScore', '?')}"
                                        ),
                                    })
                                results[team_name]["recent_results"] = (
                                    recent)
            except Exception as e:
                results[team_name] = {"error": str(e)}

        results["source"] = "TheSportsDB"
        return results

    @staticmethod
    def _extract_team_names(question: str) -> list[str]:
        """
        Extract likely team names from a market question.
        e.g. 'Will the Lakers win?' → ['Lakers']
        e.g. 'Lakers vs Heat' → ['Lakers', 'Heat']
        """
        # Common patterns: "X vs Y", "X vs. Y", "Will X win"
        vs_match = re.search(
            r"([A-Z][a-zA-Z\s]+?)\s+vs\.?\s+([A-Z][a-zA-Z\s]+?)(?:\s|$|\?|:)",
            question
        )
        if vs_match:
            return [vs_match.group(1).strip(), vs_match.group(2).strip()]

        # "Will [team] win"
        will_match = re.search(
            r"Will\s+(?:the\s+)?(.+?)\s+win", question, re.IGNORECASE)
        if will_match:
            return [will_match.group(1).strip()]

        # Spread: "Team (-X.5)"
        spread_match = re.search(
            r"Spread:\s+(.+?)\s+\(-?\d", question, re.IGNORECASE)
        if spread_match:
            return [spread_match.group(1).strip()]

        # Fallback: first capitalized phrase
        caps = re.findall(r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*", question)
        return caps[:2] if caps else []

    # ── Politics ──────────────────────────────────────────────────

    @classmethod
    def _fetch_politics(cls, question: str) -> dict:
        """Lightweight political context — key dates, incumbents."""
        context = {
            "source": "static_political_context",
            "note": (
                "No free real-time polling API available. "
                "Using market question context only."
            ),
            "current_us_president": "Donald Trump (since Jan 2025)",
            "next_us_election": "November 2028",
            "current_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

        # Add keyword-specific context
        q_lower = question.lower()
        if "fed" in q_lower or "rate" in q_lower:
            try:
                # FRED API for current fed rate (free, no key for basic)
                r = requests.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id": "FEDFUNDS",
                        "api_key": "DEMO_KEY",
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": "1",
                    },
                    timeout=cls._TIMEOUT,
                )
                if r.status_code == 200:
                    obs = r.json().get("observations", [])
                    if obs:
                        context["current_fed_rate"] = obs[0].get("value")
                        context["fed_rate_date"] = obs[0].get("date")
            except Exception:
                pass

        if "netanyahu" in q_lower or "israel" in q_lower:
            context["israel_pm"] = "Benjamin Netanyahu"

        return context

    # ── Weather ───────────────────────────────────────────────────

    @classmethod
    def _fetch_weather(cls, question: str) -> dict:
        """Open-Meteo free API — weather data."""
        # Try to extract location — very basic
        # Default to major city
        lat, lon = 40.7128, -74.0060  # NYC default
        city = "New York"

        cities = {
            "london": (51.5074, -0.1278),
            "paris": (48.8566, 2.3522),
            "tokyo": (35.6762, 139.6503),
            "los angeles": (34.0522, -118.2437),
            "chicago": (41.8781, -87.6298),
            "bangkok": (13.7563, 100.5018),
            "dubai": (25.2048, 55.2708),
            "new york": (40.7128, -74.0060),
        }
        q_lower = question.lower()
        for c, (lt, ln) in cities.items():
            if c in q_lower:
                lat, lon = lt, ln
                city = c.title()
                break

        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_sum,weathercode"
                ),
                "timezone": "auto",
                "forecast_days": "7",
            },
            timeout=cls._TIMEOUT,
        )
        if r.status_code != 200:
            return {"error": f"Open-Meteo {r.status_code}"}

        data = r.json().get("daily", {})
        return {
            "source": "Open-Meteo",
            "city": city,
            "forecast_dates": data.get("time", []),
            "temp_max_c": data.get("temperature_2m_max", []),
            "temp_min_c": data.get("temperature_2m_min", []),
            "precipitation_mm": data.get("precipitation_sum", []),
        }

    # ── Economy ───────────────────────────────────────────────────

    @classmethod
    def _fetch_economy(cls, question: str) -> dict:
        """Economic context — crude oil, basic macro."""
        context = {
            "source": "static_macro_context",
            "current_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

        q_lower = question.lower()
        if "crude" in q_lower or "oil" in q_lower or "wti" in q_lower:
            try:
                # CoinGecko doesn't do commodities, but we can note the
                # question context
                context["note"] = (
                    "Crude oil price data not available via free API. "
                    "Use market question context."
                )
            except Exception:
                pass

        return context

    # ── General / Other ───────────────────────────────────────────

    @classmethod
    def _fetch_general(cls, question: str) -> dict:
        """Minimal context for uncategorized markets."""
        return {
            "source": "none",
            "note": "No specific data source for this category.",
            "current_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }


# =====================================================================
#  Ollama Client
# =====================================================================

class OllamaClient:
    """Minimal Ollama HTTP client for structured JSON output."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "phi3:mini",
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str) -> Optional[dict]:
        """
        Send prompt to Ollama, parse JSON response.
        Returns parsed dict or None on failure.
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.3,
                "num_predict": 300,
            },
        }

        try:
            r = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout,
            )
            if r.status_code != 200:
                logger.warning(
                    "Ollama returned %d: %s", r.status_code, r.text[:200])
                return None

            raw = r.json().get("response", "")
            return self._parse_json(raw)

        except requests.exceptions.ConnectionError:
            logger.warning("Ollama not reachable at %s", self.base_url)
            return None
        except requests.exceptions.Timeout:
            logger.warning("Ollama timeout after %.0fs", self.timeout)
            return None
        except Exception as e:
            logger.warning("Ollama error: %s", e)
            return None

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        """Try to parse JSON from Ollama's response."""
        # Direct parse
        try:
            data = json.loads(raw)
            if "real_probability" in data:
                return data
        except json.JSONDecodeError:
            pass

        # Try to find JSON block in the response
        match = re.search(r"\{[^{}]*\"real_probability\"[^{}]*\}", raw)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        logger.debug("Could not parse Ollama response: %s", raw[:200])
        return None


# =====================================================================
#  Probability Validator (main class — background thread)
# =====================================================================

_PROMPT_TEMPLATE = """You are a prediction market probability analyst. Given real-world data and market information, estimate the TRUE probability of this event.

EVENT: {market_question}
OUTCOME BEING BET ON: {outcome}
CURRENT POLYMARKET PRICE: {poly_price:.4f} (= market's implied probability)
WHALE BET: ${whale_usd:.2f} on {outcome} (conviction: {conviction:.2f}% of portfolio)
TODAY: {today}

REAL-WORLD DATA:
{context_str}

INSTRUCTIONS:
1. Based ONLY on the real-world data and your knowledge, estimate the true probability of {outcome}.
2. Consider whether the Polymarket price seems too high or too low.
3. A whale with high conviction has bet on {outcome} — factor this in but don't blindly follow.

Respond with ONLY this JSON (no other text):
{{"real_probability": 0.XX, "confidence": XX, "explanation": "max 3 lines", "key_sources": ["source1", "source2"]}}"""


class ProbabilityValidator:
    """
    Shadow-mode probability validator.

    Runs in a background thread, receives signals from CopyTradeEngine
    via a queue, validates them against Ollama + real-world data,
    and logs results to shadow_trades.csv.

    NEVER blocks the main bot loop.
    """

    def __init__(
        self,
        signal_queue: queue.Queue,
        model: str = "phi3:mini",
        ollama_url: str = "http://localhost:11434",
        timeout: float = 30.0,
        min_confidence: int = 70,
        edge_threshold: float = 1.45,
    ):
        self.signal_queue = signal_queue
        self.ollama = OllamaClient(
            base_url=ollama_url, model=model, timeout=timeout)
        self.min_confidence = min_confidence
        self.edge_threshold = edge_threshold

        self._thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()

        # Stats
        self.total_evaluated = 0
        self.total_would_copy = 0
        self.total_would_reject = 0
        self.total_ollama_errors = 0

        # Ensure CSV header exists
        self._init_csv()

    def _init_csv(self) -> None:
        """Create shadow CSV with headers if it doesn't exist."""
        if not SHADOW_CSV.exists():
            SHADOW_CSV.parent.mkdir(parents=True, exist_ok=True)
            with open(SHADOW_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=SHADOW_COLUMNS)
                writer.writeheader()
            logger.info("📋 Created shadow_trades.csv at %s", SHADOW_CSV)

    # ── Start / Stop ─────────────────────────────────────────────

    def start(self) -> None:
        """Start the background validation thread."""
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ProbabilityValidator",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "🧠 ProbabilityValidator started (model=%s, edge=%.2fx, "
            "confidence>%d)",
            self.ollama.model, self.edge_threshold, self.min_confidence,
        )

    def stop(self) -> None:
        """Stop the validation thread."""
        self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=5)
            logger.info(
                "🧠 ProbabilityValidator stopped. "
                "Evaluated=%d, would_copy=%d, would_reject=%d, errors=%d",
                self.total_evaluated, self.total_would_copy,
                self.total_would_reject, self.total_ollama_errors,
            )

    # ── Background Loop ──────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main loop: consume signals from queue, validate, log."""
        while not self._shutdown.is_set():
            try:
                signal_data = self.signal_queue.get(timeout=5.0)
            except queue.Empty:
                continue

            try:
                self._evaluate(signal_data)
            except Exception as e:
                logger.warning(
                    "🧠 Validator error processing signal: %s", e,
                    exc_info=True)

    # ── Core Evaluation ──────────────────────────────────────────

    def _evaluate(self, signal: dict) -> None:
        """
        Full evaluation pipeline:
        1. Detect category
        2. Fetch real-world data
        3. Query Ollama
        4. Log to shadow CSV
        """
        t0 = time.time()

        market_q = signal.get("market_question", "Unknown market")
        outcome = signal.get("outcome", "?")
        poly_price = signal.get("poly_price", 0.0)
        whale_price = signal.get("whale_price", 0.0)
        whale_usd = signal.get("whale_usd", 0.0)
        conviction = signal.get("conviction", 0.0) * 100  # to pct
        whale_label = signal.get("whale_label", "?")
        our_size_usd = signal.get("our_size_usd", 0.0)
        bot_action = signal.get("action", "UNKNOWN")

        # 1. Category detection
        category = CategoryDetector.detect(market_q, outcome)

        # 2. Fetch real-world data
        context = DataFetcher.fetch(category, market_q, outcome)
        context_str = json.dumps(context, indent=2, default=str)[:1500]

        # 3. Build prompt & query Ollama
        prompt = _PROMPT_TEMPLATE.format(
            market_question=market_q,
            outcome=outcome,
            poly_price=poly_price,
            whale_usd=whale_usd,
            conviction=conviction,
            today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            context_str=context_str,
        )

        result = self.ollama.generate(prompt)
        latency_ms = (time.time() - t0) * 1000

        # 4. Parse result
        if result:
            model_prob = float(result.get("real_probability", 0.0))
            model_conf = int(result.get("confidence", 0))
            explanation = str(result.get("explanation", ""))[:500]
            sources = result.get("key_sources", [])

            # Decision: would we copy?
            if poly_price > 0:
                edge_ratio = model_prob / poly_price
            else:
                edge_ratio = 0.0

            would_copy = (
                edge_ratio >= self.edge_threshold
                and model_conf >= self.min_confidence
            )

            self.total_evaluated += 1
            if would_copy:
                self.total_would_copy += 1
            else:
                self.total_would_reject += 1

            logger.info(
                "🧠 IA EVAL │ %s │ %s [%s] │ cat=%s │ "
                "poly=%.3f → model=%.3f (conf=%d%%) │ "
                "edge=%.2fx │ %s │ %.0fms",
                whale_label,
                outcome,
                market_q[:35],
                category,
                poly_price,
                model_prob,
                model_conf,
                edge_ratio,
                "WOULD_COPY" if would_copy else "WOULD_REJECT",
                latency_ms,
            )

        else:
            # Ollama failed — still log
            model_prob = 0.0
            model_conf = 0
            explanation = "OLLAMA_ERROR"
            sources = []
            edge_ratio = 0.0
            would_copy = False
            self.total_ollama_errors += 1
            self.total_evaluated += 1

            logger.warning(
                "🧠 IA ERROR │ %s │ %s [%s] │ Ollama no response │ %.0fms",
                whale_label, outcome, market_q[:35], latency_ms,
            )

        # 5. Write to shadow CSV
        self._write_shadow_csv({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "whale_label": whale_label,
            "market_question": market_q[:200],
            "outcome": outcome,
            "category": category,
            "poly_price": f"{poly_price:.6f}",
            "whale_price": f"{whale_price:.6f}",
            "whale_usd": f"{whale_usd:.2f}",
            "conviction_pct": f"{conviction:.4f}",
            "our_size_usd": f"{our_size_usd:.2f}",
            "model_probability": f"{model_prob:.4f}",
            "model_confidence": str(model_conf),
            "model_explanation": explanation,
            "model_sources": json.dumps(sources),
            "would_copy": str(would_copy),
            "edge_ratio": f"{edge_ratio:.4f}",
            "bot_action": bot_action,
            "actual_outcome": "",  # filled later
            "pnl_simulated": "",   # filled later
            "model_name": self.ollama.model,
            "latency_ms": f"{latency_ms:.0f}",
        })

    def _write_shadow_csv(self, row: dict) -> None:
        """Append a row to shadow_trades.csv (thread-safe)."""
        try:
            with open(SHADOW_CSV, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=SHADOW_COLUMNS)
                writer.writerow(row)
        except Exception as e:
            logger.warning("Failed to write shadow CSV: %s", e)
