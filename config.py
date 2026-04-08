"""
config.py — Merkezi Ayar Yöneticisi
====================================
.env dosyasını okur, tüm ayarları Python değişkenlerine çevirir.
Eksik veya hatalı ayar varsa anlaşılır Türkçe hata mesajı verir.
Tüm sayısal değerler Decimal türünde tutulur (float aritmetik hata yapar).

Kullanım:
    from config import cfg
    print(cfg.MAX_POSITION_USDC)   # → Decimal('10.0')
    print(cfg.DRY_RUN)             # → True
    print(cfg.PRIVATE_KEY)         # → "0x..."
"""

import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from dotenv import load_dotenv

# ── .env dosyasını bul ve yükle ─────────────────────────────────────────
_env_path = Path(__file__).resolve().parent / ".env"

if not _env_path.exists():
    print(
        "\n❌ HATA: .env dosyası bulunamadı!\n"
        f"   Beklenen konum: {_env_path}\n"
        "   Çözüm: .env dosyasını oluştur ve değerleri doldur.\n"
    )
    sys.exit(1)

load_dotenv(_env_path)


# ── Yardımcı fonksiyonlar ───────────────────────────────────────────────

def _get(key: str, default: str = None) -> str:
    """Çevre değişkenini oku. Yoksa default döndür."""
    return os.getenv(key, default)


def _get_required(key: str, hint: str = "") -> str:
    """
    Zorunlu çevre değişkenini oku.
    Yoksa veya placeholder ise anlaşılır hata ver ve çık.
    """
    value = os.getenv(key)
    if value is None or value.strip() == "" or value.strip() == "0x...":
        extra = f"\n   İpucu: {hint}" if hint else ""
        print(
            f"\n❌ HATA: Zorunlu ayar eksik → {key}\n"
            f"   .env dosyasında '{key}' değişkenini doldur.{extra}\n"
        )
        sys.exit(1)
    return value.strip()


def _get_decimal(key: str, default: str) -> Decimal:
    """
    Sayısal ayarı Decimal olarak oku.
    Geçersizse anlaşılır hata ver.
    """
    raw = _get(key, default)
    try:
        return Decimal(raw)
    except InvalidOperation:
        print(
            f"\n❌ HATA: '{key}' ayarı geçerli bir sayı değil → '{raw}'\n"
            f"   Örnek doğru değer: {default}\n"
        )
        sys.exit(1)


def _get_int(key: str, default: int) -> int:
    """Tam sayı ayar oku. Geçersizse anlaşılır hata ver."""
    raw = _get(key, str(default))
    try:
        return int(raw)
    except ValueError:
        print(
            f"\n❌ HATA: '{key}' ayarı geçerli bir tam sayı değil → '{raw}'\n"
            f"   Örnek doğru değer: {default}\n"
        )
        sys.exit(1)


def _get_bool(key: str, default: bool) -> bool:
    """Boolean ayar oku. 'true'/'1'/'yes' → True, geri kalan → False."""
    raw = _get(key, str(default)).strip().lower()
    return raw in ("true", "1", "yes")


# ── Ayar Sınıfı ─────────────────────────────────────────────────────────

class Config:
    """
    Tüm bot ayarlarını tek nesnede toplar.
    Her modül 'from config import cfg' ile erişir.
    """

    def __init__(self):
        # --- Cüzdan & API ---
        self.PRIVATE_KEY: str = _get_required(
            "POLYMARKET_PRIVATE_KEY",
            "Polymarket private key'ini almak için: reveal.polymarket.com"
        )
        self.FUNDER_ADDRESS: str = _get_required(
            "POLYMARKET_FUNDER_ADDRESS",
            "Polymarket cüzdan adresini gir (0x ile başlar)"
        )
        self.SIGNATURE_TYPE: int = _get_int("POLYMARKET_SIGNATURE_TYPE", 1)
        self.CLOB_HOST: str = _get("CLOB_HOST", "https://clob.polymarket.com")
        self.CHAIN_ID: int = _get_int("CHAIN_ID", 137)

        # --- Proxy (geoblock aşmak için, opsiyonel) ---
        # Örnek: HTTPS_PROXY=socks5h://user:pass@proxy-host:1080
        self.HTTPS_PROXY: str = _get("HTTPS_PROXY", "")
        if self.HTTPS_PROXY:
            import os
            os.environ["HTTPS_PROXY"] = self.HTTPS_PROXY
            os.environ["HTTP_PROXY"] = self.HTTPS_PROXY
            print(f"  [Proxy] Aktif: {self.HTTPS_PROXY.split('@')[-1]}")

        # --- Polygon RPC (Oracle için) ---
        self.POLYGON_RPC_URL: str = _get("POLYGON_RPC_URL", "https://polygon-rpc.com")
        self.POLYGON_RPC_BACKUP: str = _get("POLYGON_RPC_BACKUP", "")
        self.POLYGON_RPC_FALLBACK2: str = _get("POLYGON_RPC_FALLBACK2", "")
        # Ücretsiz RPC'lerde 429 riski: oracle sıklığını düşük tut
        self.ORACLE_POLL_INTERVAL_SECONDS: int = _get_int(
            "ORACLE_POLL_INTERVAL_SECONDS", 12
        )

        # --- Strateji Parametreleri ---
        self.DELTA_THRESHOLD: Decimal = _get_decimal("DELTA_THRESHOLD_PERCENT", "0.04")
        self.ORACLE_LAG_MIN: int = _get_int("ORACLE_LAG_MIN_SECONDS", 10)
        self.ORACLE_LAG_FRESH: int = _get_int("ORACLE_LAG_FRESH_SECONDS", 5)
        self.EXCHANGE_DIVERGENCE_MAX: Decimal = _get_decimal("EXCHANGE_DIVERGENCE_MAX", "0.1")

        # --- Risk Yönetimi ---
        self.MAX_POSITION: Decimal = _get_decimal("MAX_POSITION_USDC", "10.0")
        self.DAILY_LOSS_LIMIT: Decimal = _get_decimal("DAILY_LOSS_LIMIT_USDC", "50.0")
        self.MAX_TOKEN_PRICE: Decimal = _get_decimal("MAX_TOKEN_PRICE", "0.92")
        self.MIN_TOKEN_PRICE: Decimal = _get_decimal("MIN_TOKEN_PRICE", "0.55")
        self.ENTRY_WINDOW_START: int = _get_int("ENTRY_WINDOW_START_SECONDS", 30)
        self.ENTRY_WINDOW_END: int = _get_int("ENTRY_WINDOW_END_SECONDS", 10)
        self.COOLDOWN_LOSSES: int = _get_int("COOLDOWN_AFTER_CONSECUTIVE_LOSSES", 3)
        self.COOLDOWN_WINDOWS: int = _get_int("COOLDOWN_WINDOWS", 3)

        # --- Production Güvenlik ---
        self.EMERGENCY_BALANCE_MIN: Decimal = _get_decimal("EMERGENCY_BALANCE_MIN", "3.0")
        self.EMERGENCY_CONSECUTIVE_LOSSES: int = _get_int("EMERGENCY_CONSECUTIVE_LOSSES", 5)
        self.EMERGENCY_ORDER_REJECTS: int = _get_int("EMERGENCY_ORDER_REJECTS", 3)
        self.PROTECTION_TRADES: int = _get_int("PROTECTION_TRADES", 10)
        self.PROTECTION_DELAY: int = _get_int("PROTECTION_DELAY_SECONDS", 5)

        # --- Kelly Criterion ---
        self.USE_KELLY: bool = _get_bool("USE_KELLY", False)
        self.KELLY_FRACTION: Decimal = _get_decimal("KELLY_FRACTION", "0.25")
        self.KELLY_MAX_BET_PCT: Decimal = _get_decimal("KELLY_MAX_BET_PCT", "0.20")

        # --- Multi-Asset ---
        _assets_raw = _get("TRADING_ASSETS", "BTC")
        self.TRADING_ASSETS: list[str] = [
            a.strip().upper() for a in _assets_raw.split(",") if a.strip()
        ]

        # --- Trading Filtreleri ---
        self.TRADING_HOURS_ENABLED: bool = _get_bool("TRADING_HOURS_ENABLED", False)
        self.TRADING_HOUR_START: int = _get_int("TRADING_HOUR_START", 6)   # ET saat
        self.TRADING_HOUR_END: int = _get_int("TRADING_HOUR_END", 23)      # ET saat
        self.DAILY_MOVE_FILTER_ENABLED: bool = _get_bool("DAILY_MOVE_FILTER_ENABLED", False)
        self.DAILY_MOVE_THRESHOLD_PCT: Decimal = _get_decimal("DAILY_MOVE_THRESHOLD_PCT", "3.0")

        # --- Sinyal Kalitesi ---
        self.MIN_CONFIDENCE: int = _get_int("MIN_CONFIDENCE", 70)
        self.MIN_MARKET_SPREAD: Decimal = _get_decimal("MIN_MARKET_SPREAD", "0.06")

        # --- Telegram Bildirimleri (opsiyonel) ---
        self.TELEGRAM_ENABLED: bool = _get_bool("TELEGRAM_ENABLED", False)
        self.TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID: str = _get("TELEGRAM_CHAT_ID", "")
        # all | results | critical — ayrıntı: run.py _telegram_should_notify
        self.TELEGRAM_NOTIFY_MODE: str = _get(
            "TELEGRAM_NOTIFY_MODE", "all"
        ).strip().lower()

        # --- Dry Run ---
        self.DRY_RUN: bool = _get_bool("DRY_RUN", True)
        self.DRY_RUN_BALANCE: Decimal = _get_decimal("DRY_RUN_BALANCE", "1000")

        # --- Log & Dosya ---
        self.LOG_FILE: str = _get("LOG_FILE", "data/trades.jsonl")

        # --- Proje Kök Dizini ---
        self.PROJECT_ROOT: Path = Path(__file__).resolve().parent

    def print_summary(self):
        """Başlangıçta konsola ayar özetini yazdırır."""
        mode = "KURU CALISMA (DRY RUN)" if self.DRY_RUN else "GERCEK ISLEM MODU"
        wallet = f"{self.FUNDER_ADDRESS[:8]}...{self.FUNDER_ADDRESS[-4:]}"
        assets_str = ",".join(self.TRADING_ASSETS)
        print(f"""
+==================================================+
|           POLYMARKET ORACLE ARB BOT              |
+==================================================+
|  Mod:              {mode:<28s} |
|  Cuzdan:           {wallet:<28s} |
|  CLOB:             {self.CLOB_HOST:<28s} |
|  RPC:              {str(self.POLYGON_RPC_URL)[:28]:<28s} |
+--------------------------------------------------+
|  Varliklar:        {assets_str:<28s} |
|  Delta esigi:      %{str(self.DELTA_THRESHOLD):<27s} |
|  Oracle lag min:   {self.ORACLE_LAG_MIN}sn{'':<25s} |
|  Maks pozisyon:    ${str(self.MAX_POSITION):<27s} |
|  Gunluk kayip max: ${str(self.DAILY_LOSS_LIMIT):<27s} |
|  Token fiyat:      ${str(self.MIN_TOKEN_PRICE)} - ${str(self.MAX_TOKEN_PRICE):<21s} |
|  Emir penceresi:   T-{self.ENTRY_WINDOW_START}s -> T-{self.ENTRY_WINDOW_END}s{'':<20s} |
|  Ard arda kayip:   {self.COOLDOWN_LOSSES} -> {self.COOLDOWN_WINDOWS} pencere mola{'':<14s} |
+==================================================+
""")


# ── Tek global instance oluştur ─────────────────────────────────────────
cfg = Config()

# ── Doğrudan çalıştırılırsa ayarları göster ─────────────────────────────
if __name__ == "__main__":
    cfg.print_summary()
    print("Tum ayarlar basariyla yuklendi.")
