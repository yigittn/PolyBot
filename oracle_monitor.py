"""
oracle_monitor.py — Chainlink Oracle Takibi
=============================================
Polygon ağı üzerindeki Chainlink BTC/USD fiyat oracle'ını okur.
Son fiyat, son güncelleme zamanı ve gecikme (lag) bilgisi verir.

Chainlink BTC/USD on Polygon:
  - Aggregator: 0xc907E116054Ad103354f2D350FD2514433D57F6f
  - latestRoundData() → (roundId, answer, startedAt, updatedAt, answeredInRound)
  - Fiyat = answer / 10^8

Kullanım:
    from oracle_monitor import OracleMonitor
    monitor = OracleMonitor(cfg)
    await monitor.start()
    data = monitor.get_oracle_data()  # → dict
"""

import asyncio
import time
from decimal import Decimal
from typing import Optional

from web3 import Web3

from config import cfg

# Chainlink BTC/USD Aggregator kontrat adresi (Polygon)
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"

# Sadece latestRoundData() fonksiyonu gerekli — minimal ABI
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
    """Chainlink BTC/USD oracle'ını Polygon üzerinden okur."""

    def __init__(self, config=None):
        self._cfg = config or cfg
        self._w3: Optional[Web3] = None
        self._contract = None
        self._rpc_urls: list[str] = []
        self._rpc_idx: int = 0
        self._last_oracle_error_log: float = 0.0

        # Güncel oracle verisi
        self._price: Optional[Decimal] = None
        self._updated_at: int = 0
        self._round_id: int = 0

        # Pencere açılış fiyatı cache'i
        # {window_ts: Decimal} formatında
        self._window_open_cache: dict[int, Decimal] = {}

        # Arka plan güncelleme task'ı
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
        Web3 bağlantısı kur ve arka planda oracle'ı polling ile güncelle.
        Birden fazla RPC URL sırayla denenir.
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

        await self._fetch_latest()

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
        """Verilen RPC URL'ye Web3 bağlantısı kur. Başarılıysa True döndür."""
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                self._w3 = w3
                self._contract = w3.eth.contract(
                    address=Web3.to_checksum_address(CHAINLINK_BTC_USD),
                    abi=AGGREGATOR_ABI,
                )
                return True
        except Exception:
            pass
        return False

    # ── Oracle Veri Sorgulama ────────────────────────────────────────────

    def get_oracle_data(self) -> dict:
        """
        Güncel oracle verisini döndürür.

        Dönüş:
            {
              "price": Decimal("87498.12"),
              "updated_at": 1743173680,
              "lag_seconds": 22,
              "round_id": 36893488147419145678,
              "is_fresh": False
            }
        """
        now = int(time.time())
        lag = now - self._updated_at if self._updated_at > 0 else 999

        return {
            "price": self._price,
            "updated_at": self._updated_at,
            "lag_seconds": lag,
            "round_id": self._round_id,
            "is_fresh": lag < self._cfg.ORACLE_LAG_FRESH,
        }

    def get_window_open_price(self, window_ts: int) -> Optional[Decimal]:
        """
        Belirtilen 5-dk penceresinin açılışındaki oracle fiyatını döndürür.
        Bot pencere açılışında oracle'ı okur ve cache'ler.
        Cache'de yoksa None döner.
        """
        return self._window_open_cache.get(window_ts)

    def cache_window_open_price(self, window_ts: int):
        """
        Şu anki oracle fiyatını pencere açılış fiyatı olarak kaydet.
        run.py pencere başlangıcında bunu çağırır.
        """
        if self._price is not None:
            self._window_open_cache[window_ts] = self._price
            # Eski cache'leri temizle (son 10 pencere yeterli)
            if len(self._window_open_cache) > 10:
                oldest_key = min(self._window_open_cache.keys())
                del self._window_open_cache[oldest_key]

    # ── Arka Plan Güncelleme ─────────────────────────────────────────────

    async def _poll_loop(self, interval_sec: int):
        """
        Oracle'ı seyrek poll et (429 riskini azaltır).
        429 / rate limit → uzun bekleme + sıradaki RPC.
        """
        retry_count = 0
        while self._running:
            try:
                await self._fetch_latest()
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
                    # Son okunan fiyatı KORUYORUZ — None yapma! Lag hesabı
                    # zaten updated_at'ten çalışır; eski fiyat > fiyat yok.
                    retry_count = 0
                    await asyncio.sleep(30)

    async def _fetch_latest(self):
        """
        Chainlink'ten latestRoundData() çağır ve verileri güncelle.
        Async loop içinde blocking call'u thread'e devreder.
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, self._contract.functions.latestRoundData().call
        )
        # result = (roundId, answer, startedAt, updatedAt, answeredInRound)
        round_id, answer, _, updated_at, _ = result

        # Fiyat = answer / 10^8 (Chainlink 8 decimal kullanır)
        self._price = Decimal(answer) / Decimal(10 ** 8)
        self._updated_at = updated_at
        self._round_id = round_id
