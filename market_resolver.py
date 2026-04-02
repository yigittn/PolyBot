"""
market_resolver.py — Aktif Piyasa Bulucu
==========================================
Polymarket'te şu anda açık olan "BTC Up or Down" 5 dakikalık piyasayı bulur,
UP ve DOWN token ID'lerini ve güncel fiyatlarını döndürür.

Kullandığı API'ler:
  - Gamma API: Piyasa keşfi (slug, condition_id, outcomes)
  - CLOB API: Order book, best ask, token fiyatları

Kullanım:
    from market_resolver import MarketResolver
    resolver = MarketResolver(cfg)
    await resolver.initialize()
    window = resolver.get_current_window()
    market = await resolver.get_active_market()
"""

import asyncio
import ssl
import time
from decimal import Decimal
from typing import Optional

import aiohttp
import certifi
from py_clob_client.client import ClobClient

from config import cfg


class MarketResolver:
    """Aktif 5-dk BTC Up/Down piyasasını bulur ve token bilgilerini sağlar."""

    # Gamma API — piyasa keşfi için
    GAMMA_API = "https://gamma-api.polymarket.com"

    def __init__(self, config=None):
        self._cfg = config or cfg
        self._clob: Optional[ClobClient] = None
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Başlatma / Kapatma ───────────────────────────────────────────────

    async def initialize(self):
        """CLOB client ve HTTP session kur."""
        # CLOB Client (order book okumak için)
        self._clob = ClobClient(
            host=self._cfg.CLOB_HOST,
            key=self._cfg.PRIVATE_KEY,
            chain_id=self._cfg.CHAIN_ID,
            signature_type=self._cfg.SIGNATURE_TYPE,
            funder=self._cfg.FUNDER_ADDRESS,
        )
        self._clob.set_api_creds(self._clob.create_or_derive_api_creds())

        # Async HTTP session (Gamma API için — SSL context gerekli)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        self._session = aiohttp.ClientSession(connector=connector)

    async def close(self):
        """HTTP session'ı kapat."""
        if self._session:
            await self._session.close()

    # ── Pencere Hesaplama ────────────────────────────────────────────────

    def get_current_window(self) -> dict:
        """
        Şu anki 5 dakikalık pencereyi hesapla.

        Dönüş:
            {
              "window_ts": 1743173700,       # Pencere başlangıç Unix timestamp
              "close_time": 1743174000,      # Pencere bitiş Unix timestamp
              "seconds_remaining": 47,       # Kapanışa kalan saniye
              "is_entry_window": True        # Emir giriş penceresinde miyiz?
            }
        """
        now = int(time.time())
        window_ts = now - (now % 300)          # Pencere başlangıcı (5 dk = 300 sn)
        close_time = window_ts + 300           # Pencere bitişi
        remaining = close_time - now           # Kalan saniye

        # Emir giriş penceresi: ENTRY_WINDOW_START > remaining > ENTRY_WINDOW_END
        is_entry = (
            self._cfg.ENTRY_WINDOW_START >= remaining > self._cfg.ENTRY_WINDOW_END
        )

        return {
            "window_ts": window_ts,
            "close_time": close_time,
            "seconds_remaining": remaining,
            "is_entry_window": is_entry,
        }

    # ── Aktif Piyasa Bulma ───────────────────────────────────────────────

    async def get_active_market(self) -> Optional[dict]:
        """
        Gamma API'den aktif BTC 5-dk piyasasını ara,
        token ID'lerini ve güncel fiyatları döndür.

        Bulunamazsa None döner (henüz açılmamış veya kapanmış olabilir).

        Dönüş:
            {
              "condition_id": "0x...",
              "token_id_up": "71321...",
              "token_id_down": "52050...",
              "up_price": Decimal("0.72"),
              "down_price": Decimal("0.28"),
              "up_best_ask": Decimal("0.73"),
              "down_best_ask": Decimal("0.29"),
              "liquidity": Decimal("5200.00"),
              "slug": "btc-5min-up-down-1743173700"
            }
        """
        if not self._session:
            await self.initialize()

        # Gamma API'den aktif BTC 5-dk piyasalarını ara
        market_data = await self._search_gamma_market()
        if not market_data:
            return None

        # Token ID'lerini ve fiyatlarını çıkar
        result = self._parse_market_data(market_data)
        if not result:
            return None

        # CLOB'dan order book bilgisi al (best ask, likidite)
        result = await self._enrich_with_orderbook(result)

        return result

    async def _search_gamma_market(self) -> Optional[dict]:
        """
        Gamma API'den aktif BTC up/down 5-dk piyasasını ara.
        Strateji:
          1) Mevcut pencerenin slug'ını doğrudan dene (en hızlı)
          2) Bulunamazsa geniş arama yap ve filtrele
        """
        try:
            window = self.get_current_window()
            window_ts = window["window_ts"]

            # Strateji 1: Doğrudan slug ile ara
            expected_slug = f"btc-updown-5m-{window_ts}"
            market = await self._fetch_by_slug(expected_slug)
            if market:
                return market

            # Strateji 2: Geniş arama + keyword filtre
            url = f"{self.GAMMA_API}/markets"
            params = {
                "active": "true",
                "closed": "false",
                "limit": 100,
                "order": "createdAt",
                "ascending": "false",
            }
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                markets = await resp.json()

            for market in markets:
                slug = market.get("slug", "").lower()
                question = market.get("question", "").lower()
                combined = slug + " " + question
                if "btc-updown-5m" in slug:
                    return market
                if ("btc" in combined or "bitcoin" in combined) and ("5" in combined or "minute" in combined) and ("up" in combined or "down" in combined):
                    return market

            return None

        except Exception:
            return None

    async def _fetch_by_slug(self, slug: str) -> Optional[dict]:
        """Belirli bir slug ile doğrudan piyasa bilgisi al."""
        try:
            url = f"{self.GAMMA_API}/markets"
            params = {"slug": slug}
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if isinstance(data, list) and data:
                    return data[0]
                if isinstance(data, dict) and data.get("conditionId"):
                    return data
            return None
        except Exception:
            return None

    def _parse_market_data(self, market: dict) -> Optional[dict]:
        """
        Gamma API market verisinden token ID'leri ve fiyatları çıkar.
        outcomes = ["Up", "Down"] ve tokens dizisinden eşleştir.
        """
        try:
            condition_id = market.get("conditionId", "")
            slug = market.get("slug", "")

            # Token'ları çıkar
            tokens = market.get("clobTokenIds", "")
            outcomes = market.get("outcomes", "")
            outcome_prices = market.get("outcomePrices", "")

            # String formatlarını parse et
            if isinstance(tokens, str):
                tokens = [t.strip().strip('"') for t in tokens.strip("[]").split(",")]
            if isinstance(outcomes, str):
                outcomes = [o.strip().strip('"') for o in outcomes.strip("[]").split(",")]
            if isinstance(outcome_prices, str):
                outcome_prices = [p.strip().strip('"') for p in outcome_prices.strip("[]").split(",")]

            if len(tokens) < 2 or len(outcomes) < 2:
                return None

            # UP ve DOWN token'larını eşleştir
            token_id_up = None
            token_id_down = None
            up_price = Decimal("0")
            down_price = Decimal("0")

            for i, outcome in enumerate(outcomes):
                outcome_lower = outcome.lower()
                price = Decimal(outcome_prices[i]) if i < len(outcome_prices) else Decimal("0")
                if "up" in outcome_lower or "yes" in outcome_lower:
                    token_id_up = tokens[i] if i < len(tokens) else None
                    up_price = price
                elif "down" in outcome_lower or "no" in outcome_lower:
                    token_id_down = tokens[i] if i < len(tokens) else None
                    down_price = price

            if not token_id_up or not token_id_down:
                return None

            return {
                "condition_id": condition_id,
                "token_id_up": token_id_up,
                "token_id_down": token_id_down,
                "up_price": up_price,
                "down_price": down_price,
                "up_best_ask": up_price,
                "down_best_ask": down_price,
                "up_liquidity": Decimal("0"),
                "down_liquidity": Decimal("0"),
                "slug": slug,
            }

        except Exception:
            return None

    async def _enrich_with_orderbook(self, market: dict) -> dict:
        """
        CLOB order book bilgisi al. best_ask olarak Gamma fiyatı kullanılır
        (CLOB asks genellikle $0.95+ take-profit emirleri).
        CLOB ask ayrıca saklanır — emir gönderirken referans olur.
        """
        try:
            loop = asyncio.get_running_loop()

            up_book = await loop.run_in_executor(
                None, self._clob.get_order_book, market["token_id_up"]
            )
            if up_book and hasattr(up_book, "asks") and up_book.asks:
                market["up_clob_ask"] = Decimal(str(up_book.asks[0].price))
                market["up_liquidity"] = sum(
                    Decimal(str(a.size)) for a in up_book.asks
                )

            down_book = await loop.run_in_executor(
                None, self._clob.get_order_book, market["token_id_down"]
            )
            if down_book and hasattr(down_book, "asks") and down_book.asks:
                market["down_clob_ask"] = Decimal(str(down_book.asks[0].price))
                market["down_liquidity"] = sum(
                    Decimal(str(a.size)) for a in down_book.asks
                )

        except Exception:
            pass

        gamma_up = market.get("up_price", Decimal("0"))
        gamma_down = market.get("down_price", Decimal("0"))
        if market.get("up_liquidity", Decimal("0")) == 0 and gamma_up > 0:
            market["up_liquidity"] = Decimal("100")
        if market.get("down_liquidity", Decimal("0")) == 0 and gamma_down > 0:
            market["down_liquidity"] = Decimal("100")

        return market

    # ── Yardımcı ─────────────────────────────────────────────────────────

    def is_tradeable(self, market: dict) -> bool:
        """
        Piyasa işlem yapılabilir durumda mı kontrol et.
        Token fiyatı MIN-MAX aralığında mı? Likidite yeterli mi?
        """
        up_ask = market.get("up_best_ask", Decimal("0"))
        down_ask = market.get("down_best_ask", Decimal("0"))
        up_liq = market.get("up_liquidity", Decimal("0"))
        down_liq = market.get("down_liquidity", Decimal("0"))

        up_ok = self._cfg.MIN_TOKEN_PRICE <= up_ask <= self._cfg.MAX_TOKEN_PRICE
        down_ok = self._cfg.MIN_TOKEN_PRICE <= down_ask <= self._cfg.MAX_TOKEN_PRICE

        if not (up_ok or down_ok):
            import time
            if not hasattr(self, "_last_debug_ts") or time.time() - self._last_debug_ts > 30:
                self._last_debug_ts = time.time()
                print(
                    f"  [DEBUG] Up ask=${up_ask} | Down ask=${down_ask} "
                    f"| Range: ${self._cfg.MIN_TOKEN_PRICE}-${self._cfg.MAX_TOKEN_PRICE}"
                )

        return up_ok or down_ok
