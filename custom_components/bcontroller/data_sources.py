"""External data sources for BController's intelligence layer.

Provides:
  - RSS news headlines from CoinDesk, CoinTelegraph, Decrypt
  - Fear & Greed Index from Alternative.me
  - Technical indicators (RSI, MACD, BB, EMA) via pandas-ta from kline data

All network I/O is dispatched via hass.async_add_executor_job.
Results are cached in-memory with per-source TTLs.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# ─── Cache TTLs ───────────────────────────────────────────────────────────────
_NEWS_TTL_SECONDS: int = 300          # 5 minutes
_FEAR_GREED_TTL_SECONDS: int = 3600  # 1 hour (index updates daily)

# ─── News feed URLs ───────────────────────────────────────────────────────────
_NEWS_FEEDS: dict[str, str] = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "decrypt": "https://decrypt.co/feed",
}
_MAX_HEADLINES_PER_SOURCE: int = 5
_NEWS_LOOKBACK_SECONDS: int = 7200   # 2 hours

# ─── Fear & Greed API ────────────────────────────────────────────────────────
_FEAR_GREED_URL: str = "https://api.alternative.me/fng/?limit=1"


class DataSourceError(Exception):
    """Raised when a data source fetch fails and no cached data is available."""


class NewsCache:
    """Thread-safe in-memory cache for RSS news headlines."""

    def __init__(self) -> None:
        self._data: dict[str, list[str]] = {}
        self._fetched_at: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return time.monotonic() - self._fetched_at < _NEWS_TTL_SECONDS

    def get(self) -> dict[str, list[str]]:
        return self._data

    def set(self, data: dict[str, list[str]]) -> None:
        self._data = data
        self._fetched_at = time.monotonic()


class FearGreedCache:
    """Thread-safe in-memory cache for Fear & Greed Index."""

    def __init__(self) -> None:
        self._value: int = 50
        self._label: str = "Neutral"
        self._fetched_at: float = 0.0

    @property
    def is_fresh(self) -> bool:
        return time.monotonic() - self._fetched_at < _FEAR_GREED_TTL_SECONDS

    def get(self) -> tuple[int, str]:
        return self._value, self._label

    def set(self, value: int, label: str) -> None:
        self._value = value
        self._label = label
        self._fetched_at = time.monotonic()


class DataSources:
    """Aggregates all external data for the intelligence layer.

    Maintains per-source caches and dispatches blocking I/O to executor threads.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._news_cache = NewsCache()
        self._fg_cache = FearGreedCache()

    # ─── RSS News ─────────────────────────────────────────────────────────

    def _fetch_news_sync(self) -> dict[str, list[str]]:
        """Synchronous: fetch headlines from all RSS feeds.

        Called in an executor thread.  Returns a dict keyed by source name,
        each value a list of headline strings from the last 2 hours.
        """
        import time as _time

        try:
            import feedparser  # type: ignore[import]
        except ImportError:
            _LOGGER.warning("feedparser not installed — news source unavailable")
            return {}

        cutoff = _time.time() - _NEWS_LOOKBACK_SECONDS
        results: dict[str, list[str]] = {}

        for source, url in _NEWS_FEEDS.items():
            try:
                feed = feedparser.parse(url)
                headlines: list[str] = []
                for entry in feed.entries:
                    # feedparser stores publication time in entry.published_parsed
                    # (struct_time) or entry.updated_parsed
                    pub = getattr(entry, "published_parsed", None) or getattr(
                        entry, "updated_parsed", None
                    )
                    if pub is not None:
                        import calendar
                        entry_ts = calendar.timegm(pub)
                        if entry_ts < cutoff:
                            continue

                    title = getattr(entry, "title", "").strip()
                    if title:
                        headlines.append(title)
                    if len(headlines) >= _MAX_HEADLINES_PER_SOURCE:
                        break

                results[source] = headlines
                _LOGGER.debug(
                    "News fetch: %s returned %d headline(s)", source, len(headlines)
                )
            except Exception as exc:
                _LOGGER.warning("News fetch failed for %s: %s", source, exc)
                results[source] = []

        return results

    async def get_news_headlines(self) -> list[str]:
        """Return a flat list of recent headlines from all sources.

        Uses the cache if fresh; otherwise fetches asynchronously.
        Returns at most 5 headlines per source, deduplicated.
        """
        if not self._news_cache.is_fresh:
            try:
                raw = await self._hass.async_add_executor_job(self._fetch_news_sync)
                self._news_cache.set(raw)
            except Exception as exc:
                _LOGGER.error("News fetch executor error: %s", exc)
                if not self._news_cache.get():
                    return []

        all_headlines: list[str] = []
        seen: set[str] = set()
        for headlines in self._news_cache.get().values():
            for h in headlines:
                normalised = h.lower().strip()
                if normalised not in seen:
                    seen.add(normalised)
                    all_headlines.append(h)

        return all_headlines

    # ─── Fear & Greed Index ───────────────────────────────────────────────

    def _fetch_fear_greed_sync(self) -> tuple[int, str]:
        """Synchronous: fetch Fear & Greed index from Alternative.me.

        Called in an executor thread.
        """
        try:
            import urllib.request
            import json

            with urllib.request.urlopen(_FEAR_GREED_URL, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            entry = data["data"][0]
            value = int(entry["value"])
            label = str(entry.get("value_classification", "Neutral"))
            return value, label
        except Exception as exc:
            _LOGGER.warning("Fear & Greed fetch failed: %s", exc)
            raise

    async def get_fear_greed(self) -> tuple[int, str]:
        """Return (value, label) from Alternative.me Fear & Greed Index.

        Uses the cache if fresh (1-hour TTL).  Falls back to cached value
        on fetch failure.  Returns (50, "Neutral") if no data is available.
        """
        if not self._fg_cache.is_fresh:
            try:
                value, label = await self._hass.async_add_executor_job(
                    self._fetch_fear_greed_sync
                )
                self._fg_cache.set(value, label)
            except Exception:
                _LOGGER.warning(
                    "Using stale Fear & Greed data (age > 1h) due to fetch failure"
                )

        return self._fg_cache.get()

    # ─── Technical Indicators ─────────────────────────────────────────────

    def _compute_indicators_sync(self, klines: list[list]) -> dict[str, float]:
        """Synchronous: compute technical indicators from Binance kline data.

        Called in an executor thread.

        Args:
            klines: Raw Binance kline list.  Each element is:
                [open_time, open, high, low, close, volume, ...]

        Returns:
            Dict with keys: rsi, macd, macd_signal, macd_hist,
            bb_upper, bb_mid, bb_lower, ema20, ema50.
            Values are floats.  Returns NaN-safe fallback dict on error.
        """
        import pandas as pd

        fallback: dict[str, float] = {
            "rsi": 50.0,
            "macd": 0.0,
            "macd_signal": 0.0,
            "macd_hist": 0.0,
            "bb_upper": 0.0,
            "bb_mid": 0.0,
            "bb_lower": 0.0,
            "ema20": 0.0,
            "ema50": 0.0,
        }

        if not klines or len(klines) < 51:
            _LOGGER.warning(
                "Insufficient klines for indicators: got %d, need >= 51", len(klines)
            )
            return fallback

        try:
            import pandas_ta as ta  # type: ignore[import]
        except ImportError:
            _LOGGER.warning("pandas_ta not installed — technical indicators unavailable")
            return fallback

        try:
            # Build DataFrame from klines
            # Binance kline columns: [open_time, open, high, low, close, volume, ...]
            df = pd.DataFrame(
                klines,
                columns=[
                    "open_time", "open", "high", "low", "close", "volume",
                    "close_time", "quote_volume", "trades", "taker_buy_base",
                    "taker_buy_quote", "ignore",
                ],
            )
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.dropna(subset=["close"], inplace=True)

            close = df["close"]
            high = df["high"]
            low = df["low"]

            # RSI(14)
            rsi_series = ta.rsi(close, length=14)
            rsi_val = float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else 50.0
            if pd.isna(rsi_val):
                rsi_val = 50.0

            # MACD(12, 26, 9)
            macd_df = ta.macd(close, fast=12, slow=26, signal=9)
            if macd_df is not None and not macd_df.empty:
                macd_col = [c for c in macd_df.columns if c.startswith("MACD_") and not "s" in c.lower() and not "h" in c.lower()]
                signal_col = [c for c in macd_df.columns if "MACDs_" in c or c.startswith("MACDs")]
                hist_col = [c for c in macd_df.columns if "MACDh_" in c or c.startswith("MACDh")]

                # pandas_ta names them MACD_12_26_9, MACDs_12_26_9, MACDh_12_26_9
                def _last(series_list: list) -> float:
                    if not series_list:
                        return 0.0
                    val = float(macd_df[series_list[0]].iloc[-1])
                    return 0.0 if pd.isna(val) else val

                macd_val = _last(macd_col)
                signal_val = _last(signal_col)
                hist_val = _last(hist_col)
            else:
                macd_val = signal_val = hist_val = 0.0

            # Bollinger Bands(20, 2)
            bb_df = ta.bbands(close, length=20, std=2)
            if bb_df is not None and not bb_df.empty:
                def _bb_last(prefix: str) -> float:
                    cols = [c for c in bb_df.columns if c.startswith(prefix)]
                    if not cols:
                        return 0.0
                    val = float(bb_df[cols[0]].iloc[-1])
                    return 0.0 if pd.isna(val) else val

                bb_upper = _bb_last("BBU_")
                bb_mid = _bb_last("BBM_")
                bb_lower = _bb_last("BBL_")
            else:
                bb_upper = bb_mid = bb_lower = 0.0

            # EMA(20) and EMA(50)
            ema20_series = ta.ema(close, length=20)
            ema50_series = ta.ema(close, length=50)

            def _ema_last(s: Any) -> float:
                if s is None or s.empty:
                    return 0.0
                val = float(s.iloc[-1])
                return 0.0 if pd.isna(val) else val

            ema20_val = _ema_last(ema20_series)
            ema50_val = _ema_last(ema50_series)

            result = {
                "rsi": rsi_val,
                "macd": macd_val,
                "macd_signal": signal_val,
                "macd_hist": hist_val,
                "bb_upper": bb_upper,
                "bb_mid": bb_mid,
                "bb_lower": bb_lower,
                "ema20": ema20_val,
                "ema50": ema50_val,
            }
            _LOGGER.debug(
                "Indicators: RSI=%.2f MACD=%.4f BB=[%.2f/%.2f/%.2f] EMA20=%.2f EMA50=%.2f",
                rsi_val, macd_val, bb_upper, bb_mid, bb_lower, ema20_val, ema50_val,
            )
            return result

        except Exception as exc:
            _LOGGER.error("Technical indicator computation failed: %s", exc)
            return fallback

    async def compute_technical_indicators(
        self, klines: list[list]
    ) -> dict[str, float]:
        """Compute technical indicators from kline data asynchronously.

        Args:
            klines: Raw Binance kline list as returned by BinanceClient.get_klines().

        Returns:
            Dict with indicator values.  Fallback to neutral values on error.
        """
        return await self._hass.async_add_executor_job(
            self._compute_indicators_sync, klines
        )

    # ─── 24h ticker stats from klines ─────────────────────────────────────

    @staticmethod
    def extract_ohlcv_stats(klines: list[list]) -> dict[str, float]:
        """Extract 24h high, low, volume from the most recent 24 hourly klines.

        Args:
            klines: Raw Binance klines (1h interval).

        Returns:
            Dict with keys: high_24h, low_24h, volume_usdt_24h, current_price.
        """
        if not klines:
            return {"high_24h": 0.0, "low_24h": 0.0, "volume_usdt_24h": 0.0, "current_price": 0.0}

        # Take the last 24 entries (up to 24h)
        recent = klines[-24:]
        try:
            highs = [float(k[2]) for k in recent]
            lows = [float(k[3]) for k in recent]
            # quote_volume is index 7 (USDT volume when pair ends in USDT)
            volumes = [float(k[7]) for k in recent]
            current_price = float(klines[-1][4])  # close of last candle

            return {
                "high_24h": max(highs),
                "low_24h": min(lows),
                "volume_usdt_24h": sum(volumes),
                "current_price": current_price,
            }
        except (IndexError, ValueError) as exc:
            _LOGGER.warning("Could not extract OHLCV stats from klines: %s", exc)
            return {"high_24h": 0.0, "low_24h": 0.0, "volume_usdt_24h": 0.0, "current_price": 0.0}
