"""
exchange_feed.py — Borsa Fiyat Akışı (Binance + Coinbase + Bitstamp yedek)
============================================================================
Binance ve Coinbase WebSocket bağlantılarını açar, BTC ve ETH spot fiyatını
anlık olarak takip eder. İki borsanın ortalaması "güvenilir piyasa fiyatı"
olarak kullanılır.

Eğer iki borsa arasında büyük fark varsa (pump/dump/hata sinyali)
is_reliable=False döner ve sinyal üretilmez.

Kullanım:
    from exchange_feed import ExchangeFeed
    feed = ExchangeFeed(cfg)
    await feed.start()
    data = feed.get_price("BTC")  # → dict
    data = feed.get_price("ETH")  # → dict
    await feed.stop()
"""

import asyncio
import collections
import json
import ssl
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import certifi
import websockets

from config import cfg

# Per-asset WebSocket URLs and Coinbase/Bitstamp channel names
_ASSET_WS = {
    "BTC": {
        "binance": "wss://stream.binance.com:9443/ws/btcusdt@ticker",
        "coinbase_product": "BTC-USD",
        "bitstamp_channel": "live_trades_btcusd",
    },
    "ETH": {
        "binance": "wss://stream.binance.com:9443/ws/ethusdt@ticker",
        "coinbase_product": "ETH-USD",
        "bitstamp_channel": "live_trades_ethusd",
    },
}

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
BITSTAMP_WS = "wss://ws.bitstamp.net"


class ExchangeFeed:
    """Binance ve Coinbase'den eş zamanlı canlı fiyat alır (multi-asset)."""

    RETRY_DELAYS = [1, 3, 10]
    STALE_THRESHOLD = 30
    ROLLING_WINDOW_SECONDS = 900

    def __init__(self, config=None):
        self._cfg = config or cfg
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._assets: list[str] = self._cfg.TRADING_ASSETS

        # Per-asset state: {asset: Decimal | None}
        self._binance_price: dict[str, Optional[Decimal]] = {}
        self._binance_updated: dict[str, float] = {}
        self._coinbase_price: dict[str, Optional[Decimal]] = {}
        self._coinbase_updated: dict[str, float] = {}
        self._bitstamp_price: dict[str, Optional[Decimal]] = {}
        self._bitstamp_updated: dict[str, float] = {}
        self._price_history: dict[str, collections.deque] = {}

        for asset in self._assets:
            self._binance_price[asset] = None
            self._binance_updated[asset] = 0
            self._coinbase_price[asset] = None
            self._coinbase_updated[asset] = 0
            self._bitstamp_price[asset] = None
            self._bitstamp_updated[asset] = 0
            self._price_history[asset] = collections.deque()

        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ── Başlatma / Durdurma ──────────────────────────────────────────────

    async def start(self):
        """Tüm WebSocket bağlantılarını arka planda başlat."""
        self._running = True
        tasks = []
        for asset in self._assets:
            ws_cfg = _ASSET_WS.get(asset)
            if not ws_cfg:
                continue
            tasks.append(
                asyncio.create_task(
                    self._run_binance(asset, ws_cfg["binance"]),
                    name=f"ws-binance-{asset}",
                )
            )
        # Coinbase: tek bağlantı, tüm asset'ler
        cb_products = [
            _ASSET_WS[a]["coinbase_product"]
            for a in self._assets if a in _ASSET_WS
        ]
        if cb_products:
            tasks.append(
                asyncio.create_task(
                    self._run_coinbase(cb_products), name="ws-coinbase"
                )
            )
        # Bitstamp: tek bağlantı, tüm asset'ler
        bs_channels = [
            _ASSET_WS[a]["bitstamp_channel"]
            for a in self._assets if a in _ASSET_WS
        ]
        if bs_channels:
            tasks.append(
                asyncio.create_task(
                    self._run_bitstamp(bs_channels), name="ws-bitstamp"
                )
            )
        self._tasks = tasks
        await asyncio.sleep(2)

    async def stop(self):
        """Tüm WebSocket bağlantılarını temiz kapat."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    # ── Fiyat Sorgulama ─────────────────────────────────────────────────

    def get_price(self, asset: str = "BTC") -> dict:
        """
        Belirtilen asset için güncel fiyat verisini döndürür.
        İki ana kaynak (Binance + Coinbase) ortalamasını hesaplar.
        """
        now = time.time()
        bp = self._binance_price.get(asset)
        bu = self._binance_updated.get(asset, 0)
        cp = self._coinbase_price.get(asset)
        cu = self._coinbase_updated.get(asset, 0)
        sp = self._bitstamp_price.get(asset)
        su = self._bitstamp_updated.get(asset, 0)

        prices = []
        if bp and (now - bu) < self.STALE_THRESHOLD:
            prices.append(bp)
        if cp and (now - cu) < self.STALE_THRESHOLD:
            prices.append(cp)
        if not prices and sp and (now - su) < self.STALE_THRESHOLD:
            prices.append(sp)

        if not prices:
            return {
                "binance": bp, "coinbase": cp, "bitstamp": sp,
                "average": None, "spread": None, "divergence_pct": None,
                "is_reliable": False, "is_stale": True, "last_update": None,
            }

        average = sum(prices) / len(prices)

        if len(prices) >= 2:
            spread = abs(prices[0] - prices[1])
            divergence_pct = (spread / average) * Decimal("100")
        else:
            spread = Decimal("0")
            divergence_pct = Decimal("0")

        is_reliable = (
            len(prices) >= 2
            and divergence_pct < self._cfg.EXCHANGE_DIVERGENCE_MAX
        )

        latest_update = max(bu, cu)
        is_stale = (now - latest_update) > self.STALE_THRESHOLD if latest_update > 0 else True

        rolling = self._get_rolling_range(asset)
        trend_pct = self._get_trend_pct(asset)

        return {
            "binance": bp, "coinbase": cp, "bitstamp": sp,
            "average": average, "spread": spread,
            "divergence_pct": divergence_pct,
            "is_reliable": is_reliable, "is_stale": is_stale,
            "last_update": datetime.now(timezone.utc),
            "rolling_high_15m": rolling["high"],
            "rolling_low_15m": rolling["low"],
            "rolling_range_pct": rolling["range_pct"],
            "trend_pct": trend_pct,
        }

    # ── Trend (15dk başından sonuna yönsel hareket) ──────────────────────

    def _get_trend_pct(self, asset: str) -> "Optional[Decimal]":
        """
        Son 15 dakikadaki yönsel fiyat değişimi (%).
        Pozitif → yukarı trend, Negatif → aşağı trend.
        Veri yetersizse None.
        """
        history = self._price_history.get(asset)
        if not history or len(history) < 5:
            return None
        oldest_price = history[0][1]
        newest_price = history[-1][1]
        if oldest_price <= 0:
            return None
        return (newest_price - oldest_price) / oldest_price * Decimal("100")

    # ── Rolling High/Low ────────────────────────────────────────────────

    def _record_price(self, asset: str, price: Decimal):
        """Her fiyat güncellemesinde 15dk rolling window'a kaydet."""
        now = time.time()
        history = self._price_history.get(asset)
        if history is None:
            return
        history.append((now, price))
        cutoff = now - self.ROLLING_WINDOW_SECONDS
        while history and history[0][0] < cutoff:
            history.popleft()

    def _get_rolling_range(self, asset: str) -> dict:
        """Son 15 dakikanın high, low ve range yüzdesini hesapla."""
        history = self._price_history.get(asset)
        if not history or len(history) < 2:
            return {"high": None, "low": None, "range_pct": None}

        prices = [p for _, p in history]
        high = max(prices)
        low = min(prices)
        mid = (high + low) / 2

        if mid > 0:
            range_pct = ((high - low) / mid) * Decimal("100")
        else:
            range_pct = Decimal("0")

        return {"high": high, "low": low, "range_pct": range_pct}

    # ── Binance WebSocket (per-asset) ────────────────────────────────────

    async def _run_binance(self, asset: str, ws_url: str):
        """Binance ticker stream'ine bağlan ve fiyat güncelle."""
        retry_idx = 0
        while self._running:
            try:
                async with websockets.connect(
                    ws_url,
                    ssl=self._ssl_ctx,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    retry_idx = 0
                    while self._running:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(raw)
                        if "c" in data:
                            price = Decimal(data["c"])
                            self._binance_price[asset] = price
                            self._binance_updated[asset] = time.time()
                            self._record_price(asset, price)

            except asyncio.CancelledError:
                return
            except Exception:
                if not self._running:
                    return
                delay = self.RETRY_DELAYS[min(retry_idx, len(self.RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)
                retry_idx += 1

    # ── Coinbase WebSocket (multi-asset, tek bağlantı) ───────────────────

    async def _run_coinbase(self, product_ids: list[str]):
        """Coinbase ticker stream'ine bağlan ve fiyat güncelle."""
        _product_to_asset = {}
        for a in self._assets:
            ws_cfg = _ASSET_WS.get(a)
            if ws_cfg:
                _product_to_asset[ws_cfg["coinbase_product"]] = a

        subscribe_msg = json.dumps({
            "type": "subscribe",
            "channels": [{"name": "ticker", "product_ids": product_ids}],
        })

        retry_idx = 0
        while self._running:
            try:
                async with websockets.connect(
                    COINBASE_WS,
                    ssl=self._ssl_ctx,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    await ws.send(subscribe_msg)
                    retry_idx = 0

                    while self._running:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(raw)
                        if data.get("type") == "ticker" and "price" in data:
                            pid = data.get("product_id", "")
                            asset = _product_to_asset.get(pid)
                            if asset:
                                price = Decimal(data["price"])
                                self._coinbase_price[asset] = price
                                self._coinbase_updated[asset] = time.time()
                                self._record_price(asset, price)

            except asyncio.CancelledError:
                return
            except Exception:
                if not self._running:
                    return
                delay = self.RETRY_DELAYS[min(retry_idx, len(self.RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)
                retry_idx += 1

    # ── Bitstamp WebSocket (multi-asset, tek bağlantı) ───────────────────

    async def _run_bitstamp(self, channels: list[str]):
        """Bitstamp canlı işlem akışına bağlan (yedek kaynak)."""
        _channel_to_asset = {}
        for a in self._assets:
            ws_cfg = _ASSET_WS.get(a)
            if ws_cfg:
                _channel_to_asset[ws_cfg["bitstamp_channel"]] = a

        retry_idx = 0
        while self._running:
            try:
                async with websockets.connect(
                    BITSTAMP_WS,
                    ssl=self._ssl_ctx,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    for ch in channels:
                        await ws.send(json.dumps({
                            "event": "bts:subscribe",
                            "data": {"channel": ch},
                        }))
                    retry_idx = 0

                    while self._running:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(raw)
                        if data.get("event") == "trade":
                            ch = data.get("channel", "")
                            asset = _channel_to_asset.get(ch)
                            trade_data = data.get("data", {})
                            if asset and "price" in trade_data:
                                price = Decimal(str(trade_data["price"]))
                                self._bitstamp_price[asset] = price
                                self._bitstamp_updated[asset] = time.time()
                                self._record_price(asset, price)

            except asyncio.CancelledError:
                return
            except Exception:
                if not self._running:
                    return
                delay = self.RETRY_DELAYS[min(retry_idx, len(self.RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)
                retry_idx += 1
