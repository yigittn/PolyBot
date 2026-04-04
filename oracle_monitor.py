"""
oracle_monitor.py — Chainlink Oracle Takibi (Multi-Asset)
==========================================================
Polygon ağı üzerindeki Chainlink fiyat oracle'larını okur.
BTC/USD ve ETH/USD destekler.

Chainlink Aggregator'lar (Polygon):
  - BTC/USD: 0xc907E116054Ad103354f2D350FD2514433D57F6f
  - ETH/USD: 0xF9680D99D6C9589e2a93a78A04A279e509205945
  - latestRoundData() → (roundId, answer, startedAt, updatedAt, answeredInRound)
  - Fiyat = answer / 10^8

Kullanım:
    from oracle_monitor import OracleMonitor
    monitor = OracleMonitor(cfg)
    await monitor.start()
    data = monitor.get_oracle_data("BTC")  # → dict
    data = monitor.get_oracle_data("ETH")  # → dict
"""

import asyncio
import time
from decimal import Decimal
from typing import Optional

from web3 import Web3

from config import cfg

CHAINLINK_ORACLES = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
}

AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]


class OracleMonitor:
    """Chainlink oracle'larını Polygon üzerinden okur (multi-asset)."""

    def __init__(self, config=None):
        self._cfg = config or cfg
        self._w3: Optional[Web3] = None
        self._rpc_urls: list[str] = []
        self._rpc_idx: int = 0
        self._last_oracle_error_log: float = 0.0
        self._assets: list[str] = self._cfg.TRADING_ASSETS

        # Per-asset contracts and state
        self._contracts: dict = {}
        self._price: dict[str, Optional[Decimal]] = {}
        self._updated_at: dict[str, int] = {}
        self._round_id: dict[str, int] = {}
        self._window_open_cache: dict[str, dict[int, Decimal]] = {}

        for asset in self._assets:
            self._price[asset] = None
            self._updated_at[asset] = 0
            self._round_id[asset] = 0
            self._window_open_cache[asset] = {}

        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── Başlatma / Durdurma ──────────────────────────────────────────────

    def _build_rpc_url_list(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for u in (
            self._cfg.POLYGON_RPC_URL,
            self._cfg.POLYGON_RPC_BACKUP,
            getattr(self._cfg, "POLYGON_RPC_FALLBACK2", "") or "",
        ):
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _rotate_rpc(self) -> bool:
        """429 / kopma sonrasi siradaki RPC'ye gec."""
        if len(self._rpc_urls) < 2:
            return False
        for _ in range(len(self._rpc_urls)):
            self._rpc_idx = (self._rpc_idx + 1) % len(self._rpc_urls)
            if self._connect(self._rpc_urls[self._rpc_idx]):
                return True
        return False

    async def start(self):
        """
        Web3 bağlantısı kur ve arka planda oracle'ları polling ile güncelle.
        """
        self._rpc_urls = self._build_rpc_url_list()
        if not self._rpc_urls:
            raise ConnectionError("Hic POLYGON_RPC_URL tanimli degil.")

        connected = False
        for i, url in enumerate(self._rpc_urls):
            if self._connect(url):
                self._rpc_idx = i
                connected = True
                break
        if not connected:
            raise ConnectionError(
                "Tum Polygon RPC URL'leri basarisiz:\n  "
                + "\n  ".join(self._rpc_urls)
                + "\nCozum: .env'de POLYGON_RPC_URL / BACKUP / FALLBACK2 guncelle."
            )

        for asset in self._assets:
            await self._fetch_latest(asset)

        poll_iv = getattr(self._cfg, "ORACLE_POLL_INTERVAL_SECONDS", 12)
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(poll_iv), name="oracle-poll"
        )

    async def stop(self):
        """Polling'i durdur."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _connect(self, rpc_url: str) -> bool:
        """Verilen RPC URL'ye Web3 bağlantısı kur ve tüm oracle kontratlarını hazırla."""
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                self._w3 = w3
                self._contracts = {}
                for asset in self._assets:
                    addr = CHAINLINK_ORACLES.get(asset)
                    if addr:
                        self._contracts[asset] = w3.eth.contract(
                            address=Web3.to_checksum_address(addr),
                            abi=AGGREGATOR_ABI,
                        )
                return True
        except Exception:
            pass
        return False

    # ── Oracle Veri Sorgulama ────────────────────────────────────────────

    def get_oracle_data(self, asset: str = "BTC") -> dict:
        """Belirtilen asset için güncel oracle verisini döndürür."""
        now = int(time.time())
        updated = self._updated_at.get(asset, 0)
        lag = now - updated if updated > 0 else 999

        return {
            "price": self._price.get(asset),
            "updated_at": updated,
            "lag_seconds": lag,
            "round_id": self._round_id.get(asset, 0),
            "is_fresh": lag < self._cfg.ORACLE_LAG_FRESH,
        }

    def get_window_open_price(self, window_ts: int, asset: str = "BTC") -> Optional[Decimal]:
        """Belirtilen pencere ve asset için açılış oracle fiyatını döndürür."""
        cache = self._window_open_cache.get(asset, {})
        return cache.get(window_ts)

    def cache_window_open_price(self, window_ts: int, asset: str = "BTC"):
        """Şu anki oracle fiyatını pencere açılış fiyatı olarak kaydet."""
        price = self._price.get(asset)
        if price is not None:
            cache = self._window_open_cache.setdefault(asset, {})
            cache[window_ts] = price
            if len(cache) > 10:
                oldest_key = min(cache.keys())
                del cache[oldest_key]

    # ── Arka Plan Güncelleme ─────────────────────────────────────────────

    async def _poll_loop(self, interval_sec: int):
        """
        Oracle'ları seyrek poll et (429 riskini azaltır).
        Her döngüde tüm asset'leri sırayla günceller.
        """
        retry_count = 0
        while self._running:
            try:
                for asset in self._assets:
                    if not self._running:
                        return
                    await self._fetch_latest(asset)
                retry_count = 0
                await asyncio.sleep(max(5, interval_sec))
            except asyncio.CancelledError:
                return
            except Exception as e:
                retry_count += 1
                err_low = str(e).lower()
                rate_limited = "429" in err_low or "too many requests" in err_low
                if rate_limited:
                    self._rotate_rpc()
                    await asyncio.sleep(75)
                else:
                    wait = min(8 + 4 * retry_count, 60)
                    if retry_count >= 4:
                        self._rotate_rpc()
                    await asyncio.sleep(wait)

                if retry_count >= 25:
                    now_m = time.monotonic()
                    if now_m - self._last_oracle_error_log > 300:
                        from logger_module import log_error
                        log_error(
                            f"Oracle {retry_count}+ ardisiz basarisiz (son: {e})",
                            {"rpc": self._rpc_urls[self._rpc_idx] if self._rpc_urls else ""},
                        )
                        self._last_oracle_error_log = now_m
                    retry_count = 0
                    await asyncio.sleep(30)

    async def _fetch_latest(self, asset: str):
        """Chainlink'ten latestRoundData() çağır ve asset verisini güncelle."""
        contract = self._contracts.get(asset)
        if not contract:
            return
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, contract.functions.latestRoundData().call
        )
        round_id, answer, _, updated_at, _ = result
        self._price[asset] = Decimal(answer) / Decimal(10 ** 8)
        self._updated_at[asset] = updated_at
        self._round_id[asset] = round_id
