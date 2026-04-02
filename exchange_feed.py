"""
exchange_feed.py — Borsa Fiyat Akışı (Binance + Coinbase + Bitstamp yedek)
============================================================================
Binance ve Coinbase WebSocket bağlantılarını açar, BTC/USD(T) spot fiyatını
anlık olarak takip eder. İki borsanın ortalaması "güvenilir piyasa fiyatı"
olarak kullanılır.

Eğer iki borsa arasında büyük fark varsa (pump/dump/hata sinyali)
is_reliable=False döner ve sinyal üretilmez.

Kullanım:
    from exchange_feed import ExchangeFeed
    feed = ExchangeFeed(cfg)
    await feed.start()
    data = feed.get_price()  # → dict
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


class ExchangeFeed:
    """Binance ve Coinbase'den eş zamanlı canlı BTC fiyatı alır."""

    # WebSocket adresleri
    BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
    COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
    BITSTAMP_WS = "wss://ws.bitstamp.net"

    # Bağlantı kopması durumunda artan bekleme süreleri (saniye)
    RETRY_DELAYS = [1, 3, 10]

    # Bu kadar saniye güncelleme gelmezse "stale" (bayat) sayılır
    STALE_THRESHOLD = 30

    # 15 dakikalık rolling window (volatilite ölçümü için)
    ROLLING_WINDOW_SECONDS = 900

    def __init__(self, config=None):
        self._cfg = config or cfg
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        # Her borsa için son fiyat ve güncelleme zamanı
        self._binance_price: Optional[Decimal] = None
        self._binance_updated: float = 0

        self._coinbase_price: Optional[Decimal] = None
        self._coinbase_updated: float = 0

        self._bitstamp_price: Optional[Decimal] = None
        self._bitstamp_updated: float = 0

        # 15dk rolling high/low takibi — (timestamp, price) çiftleri
        self._price_history: collections.deque = collections.deque()

        # Çalışan WebSocket task'ları
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ── Başlatma / Durdurma ──────────────────────────────────────────────

    async def start(self):
        """Tüm WebSocket bağlantılarını arka planda başlat."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._run_binance(), name="ws-binance"),
            asyncio.create_task(self._run_coinbase(), name="ws-coinbase"),
            asyncio.create_task(self._run_bitstamp(), name="ws-bitstamp"),
        ]
        # Bağlantıların kurulması için kısa bekleme
        await asyncio.sleep(2)

    async def stop(self):
        """Tüm WebSocket bağlantılarını temiz kapat."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        # İptal edilen task'ların bitmesini bekle
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    # ── Fiyat Sorgulama ─────────────────────────────────────────────────

    def get_price(self) -> dict:
        """
        Güncel fiyat verisini döndürür.
        İki ana kaynak (Binance + Coinbase) ortalamasını hesaplar.
        Biri çökerse diğerinden devam eder ama is_reliable=False olur.

        Dönüş:
            {
              "binance": Decimal("87542.30") veya None,
              "coinbase": Decimal("87540.15") veya None,
              "bitstamp": Decimal("87541.00") veya None,
              "average": Decimal("87541.225"),
              "spread": Decimal("2.15"),
              "divergence_pct": Decimal("0.0025"),
              "is_reliable": True,
              "is_stale": False,
              "last_update": datetime
            }
        """
        now = time.time()

        # Hangi kaynaklar aktif? (Bitstamp de yedek olarak kullanılır)
        prices = []
        if self._binance_price and (now - self._binance_updated) < self.STALE_THRESHOLD:
            prices.append(self._binance_price)
        if self._coinbase_price and (now - self._coinbase_updated) < self.STALE_THRESHOLD:
            prices.append(self._coinbase_price)
        if not prices and self._bitstamp_price and (now - self._bitstamp_updated) < self.STALE_THRESHOLD:
            prices.append(self._bitstamp_price)

        # Hiç fiyat yoksa boş döndür
        if not prices:
            return {
                "binance": self._binance_price,
                "coinbase": self._coinbase_price,
                "bitstamp": self._bitstamp_price,
                "average": None,
                "spread": None,
                "divergence_pct": None,
                "is_reliable": False,
                "is_stale": True,
                "last_update": None,
            }

        # Ortalama hesapla
        average = sum(prices) / len(prices)

        # Spread ve divergence (iki borsa aktifse)
        if len(prices) >= 2:
            spread = abs(prices[0] - prices[1])
            divergence_pct = (spread / average) * Decimal("100")
        else:
            spread = Decimal("0")
            divergence_pct = Decimal("0")

        # Güvenilirlik: iki borsa aktif VE divergence düşük
        is_reliable = (
            len(prices) >= 2
            and divergence_pct < self._cfg.EXCHANGE_DIVERGENCE_MAX
        )

        # Bayatlık kontrolü
        latest_update = max(self._binance_updated, self._coinbase_updated)
        is_stale = (now - latest_update) > self.STALE_THRESHOLD

        rolling = self._get_rolling_range()

        return {
            "binance": self._binance_price,
            "coinbase": self._coinbase_price,
            "bitstamp": self._bitstamp_price,
            "average": average,
            "spread": spread,
            "divergence_pct": divergence_pct,
            "is_reliable": is_reliable,
            "is_stale": is_stale,
            "last_update": datetime.now(timezone.utc),
            "rolling_high_15m": rolling["high"],
            "rolling_low_15m": rolling["low"],
            "rolling_range_pct": rolling["range_pct"],
        }

    # ── Rolling High/Low (15dk volatilite ölçümü) ──────────────────────

    def _record_price(self, price: Decimal):
        """Her fiyat güncellemesinde 15dk rolling window'a kaydet."""
        now = time.time()
        self._price_history.append((now, price))
        cutoff = now - self.ROLLING_WINDOW_SECONDS
        while self._price_history and self._price_history[0][0] < cutoff:
            self._price_history.popleft()

    def _get_rolling_range(self) -> dict:
        """Son 15 dakikanın high, low ve range yüzdesini hesapla."""
        if len(self._price_history) < 2:
            return {"high": None, "low": None, "range_pct": None}

        prices = [p for _, p in self._price_history]
        high = max(prices)
        low = min(prices)
        mid = (high + low) / 2

        if mid > 0:
            range_pct = ((high - low) / mid) * Decimal("100")
        else:
            range_pct = Decimal("0")

        return {"high": high, "low": low, "range_pct": range_pct}

    # ── Binance WebSocket ────────────────────────────────────────────────

    async def _run_binance(self):
        """Binance BTC/USDT ticker stream'ine bağlan ve fiyat güncelle."""
        retry_idx = 0
        while self._running:
            try:
                async with websockets.connect(
                    self.BINANCE_WS,
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
                            self._binance_price = Decimal(data["c"])
                            self._binance_updated = time.time()
                            self._record_price(self._binance_price)

            except asyncio.CancelledError:
                return
            except Exception:
                if not self._running:
                    return
                delay = self.RETRY_DELAYS[min(retry_idx, len(self.RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)
                retry_idx += 1

    # ── Coinbase WebSocket ───────────────────────────────────────────────

    async def _run_coinbase(self):
        """Coinbase BTC-USD ticker stream'ine bağlan ve fiyat güncelle."""
        subscribe_msg = json.dumps({
            "type": "subscribe",
            "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}],
        })

        retry_idx = 0
        while self._running:
            try:
                async with websockets.connect(
                    self.COINBASE_WS,
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
                            self._coinbase_price = Decimal(data["price"])
                            self._coinbase_updated = time.time()
                            self._record_price(self._coinbase_price)

            except asyncio.CancelledError:
                return
            except Exception:
                if not self._running:
                    return
                delay = self.RETRY_DELAYS[min(retry_idx, len(self.RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)
                retry_idx += 1

    # ── Bitstamp WebSocket (Yedek) ───────────────────────────────────────

    async def _run_bitstamp(self):
        """Bitstamp BTC/USD canlı işlem akışına bağlan (yedek kaynak)."""
        subscribe_msg = json.dumps({
            "event": "bts:subscribe",
            "data": {"channel": "live_trades_btcusd"},
        })

        retry_idx = 0
        while self._running:
            try:
                async with websockets.connect(
                    self.BITSTAMP_WS,
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
                        if data.get("event") == "trade":
                            trade_data = data.get("data", {})
                            if "price" in trade_data:
                                self._bitstamp_price = Decimal(str(trade_data["price"]))
                                self._bitstamp_updated = time.time()
                                self._record_price(self._bitstamp_price)

            except asyncio.CancelledError:
                return
            except Exception:
                if not self._running:
                    return
                delay = self.RETRY_DELAYS[min(retry_idx, len(self.RETRY_DELAYS) - 1)]
                await asyncio.sleep(delay)
                retry_idx += 1
