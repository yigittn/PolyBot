"""
risk_manager.py — Risk Yöneticisi
====================================
Her işlem öncesi risk kontrolü yapar. Tek soru: "Bu işlemi yapayım mı?"

8 Risk Kuralı:
  1. SABİT POZİSYON BOYUTU — Her işlem MAX_POSITION_USDC kadar
  2. GÜNLÜK KAYIP LİMİTİ — Aşılırsa bot o gün durur
  3. ARDIŞIK KAYIP KORUMASI — N kayıp → M pencere mola
  4. TOKEN FİYAT FİLTRESİ — Çok pahalı/ucuz token alma
  5. DELTA MİNİMUM EŞİĞİ — signal_engine'de uygulanıyor
  6. EXCHANGE FİYAT TUTARLILIĞI — signal_engine'de uygulanıyor
  7. LİKİDİTE KONTROLÜ — Order book yetersizse pozisyonu küçült/atla
  8. ORACLE FRESHNESS — signal_engine'de uygulanıyor

Kullanım:
    from risk_manager import RiskManager
    rm = RiskManager(cfg)
    ok, reason = rm.can_trade(
        balance_usdc=Decimal("42.50"),
        token_price=Decimal("0.72"),
        book_size=Decimal("200"),
    )
    rm.record_trade("WIN", Decimal("3.88"))
"""

import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Tuple

from config import cfg


class RiskManager:
    """Pozisyon limitleri, drawdown ve ardışık kayıp koruması."""

    def __init__(self, config=None):
        self._cfg = config or cfg

        # Günlük state
        self._daily_pnl: Decimal = Decimal("0")
        self._total_trades: int = 0
        self._wins: int = 0
        self._losses: int = 0

        # Art arda kayıp/kazanç takibi
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0
        self._cooldown_until_window: int = 0  # Bu window_ts'e kadar cooldown

        # Tüm çalışma süresi boyunca en kötü/en iyi seriler
        self._max_consecutive_losses: int = 0
        self._max_consecutive_wins: int = 0

        # Toplam pencere sayacı (dry-run raporu için)
        self._total_windows: int = 0
        self._total_skips: int = 0

        # Başlangıç zamanı
        self._start_time: datetime = datetime.now(timezone.utc)

        # Günlük sıfırlama için tarih
        self._current_date: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Ana Kontrol ──────────────────────────────────────────────────────

    def can_trade(
        self,
        balance_usdc: Decimal = Decimal("999"),
        token_price: Decimal = None,
        book_size: Decimal = None,
    ) -> Tuple[bool, str]:
        """
        İşlem yapılabilir mi kontrol et. 8 kuralın 4'ü burada uygulanır.
        (Kural 5, 6, 8 signal_engine'de; Kural 1 calculate_position_size'da)

        Parametreler:
            balance_usdc: Cüzdandaki USDC bakiyesi
            token_price:  Alınacak token'ın best ask fiyatı (Kural 4)
            book_size:    Order book'taki best ask size (Kural 7)

        Dönüş:
            (True, "OK") veya (False, "Sebep açıklaması")
        """
        # Gün değişti mi kontrol et
        self._check_daily_reset()

        # ── KURAL 2: Günlük kayıp limiti ────────────────────────────────
        if self._daily_pnl < -self._cfg.DAILY_LOSS_LIMIT:
            return (
                False,
                f"Gunluk kayip limiti: ${self._daily_pnl} / "
                f"-${self._cfg.DAILY_LOSS_LIMIT} asildi. "
                f"Bot bugun islem yapmiyor."
            )

        # ── KURAL 3: Art arda kayıp cooldown'u ──────────────────────────
        if self._consecutive_losses >= self._cfg.COOLDOWN_LOSSES:
            if self._cooldown_until_window > 0:
                now = int(time.time())
                current_window = now - (now % 300)
                if current_window < self._cooldown_until_window:
                    remaining_windows = (self._cooldown_until_window - current_window) // 300
                    return (
                        False,
                        f"Cooldown: {self._consecutive_losses} ardisik kayip, "
                        f"{remaining_windows} pencere bekleniyor"
                    )
                else:
                    # Cooldown bitti — sıfırla
                    self._consecutive_losses = 0
                    self._cooldown_until_window = 0

        # ── KURAL 4: Token fiyat filtresi ────────────────────────────────
        if token_price is not None:
            if token_price > self._cfg.MAX_TOKEN_PRICE:
                return (
                    False,
                    f"Token fiyati cok yuksek: ${token_price} > "
                    f"max ${self._cfg.MAX_TOKEN_PRICE}. "
                    f"Kar marji cok dusuk, almaya degmez."
                )
            if token_price < self._cfg.MIN_TOKEN_PRICE:
                return (
                    False,
                    f"Token fiyati cok dusuk: ${token_price} < "
                    f"min ${self._cfg.MIN_TOKEN_PRICE}. "
                    f"Belirsiz bolge, avantaj yok."
                )

        # ── KURAL 7: Likidite kontrolü ──────────────────────────────────
        if book_size is not None and token_price is not None:
            # Order book'taki mevcut likidite (USDC cinsinden)
            available_usdc = book_size * token_price
            if available_usdc < Decimal("5"):
                return (
                    False,
                    f"Likidite yetersiz: order book'ta sadece "
                    f"${available_usdc:.2f} var. Minimum $5 gerekli."
                )

        # ── Bakiye: max(MAX_POSITION, 5 share × GTC limit fiyati) ──
        need = self._cfg.MAX_POSITION
        if token_price is not None and token_price > 0:
            limit_px = min(
                (token_price * Decimal("1.40")).quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN
                ),
                self._cfg.MAX_TOKEN_PRICE,
            )
            min_for_exchange = Decimal("5") * limit_px
            if min_for_exchange > need:
                need = min_for_exchange
        if balance_usdc < need:
            return (
                False,
                f"Bakiye yetersiz: ${balance_usdc}, "
                f"bu token icin en az ${need.quantize(Decimal('0.01'))} gerekli"
            )

        return (True, "OK")

    # ── Pozisyon Boyutu (KURAL 1 + KURAL 7) ─────────────────────────────

    def calculate_position_size(
        self,
        confidence: int,
        token_price: Decimal,
        book_size: Decimal = None,
    ) -> Decimal:
        """
        Pozisyon boyutunu hesapla.

        KURAL 1: Sabit pozisyon = MAX_POSITION_USDC
        KURAL 7: Order book yetersizse pozisyonu küçült

        Parametreler:
            confidence: Sinyal güvenilirlik skoru (0-100)
            token_price: Token fiyatı
            book_size: Order book best ask size (share)

        Dönüş:
            Pozisyon boyutu (USDC)
        """
        position = self._cfg.MAX_POSITION

        # KURAL 7: Likidite kontrolü — book size küçükse pozisyonu düşür
        if book_size is not None and token_price is not None and token_price > 0:
            available_usdc = book_size * token_price
            if available_usdc < position:
                # Book'taki miktarın %80'ini al (slippage payı bırak)
                position = (available_usdc * Decimal("0.8")).quantize(Decimal("0.01"))

        # Minimum emir (5 share × GTC limit fiyati, order_executor ile aynı)
        if token_price and token_price > 0:
            limit_px = min(
                (token_price * Decimal("1.40")).quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN
                ),
                self._cfg.MAX_TOKEN_PRICE,
            )
            min_cost = Decimal("5") * limit_px
        else:
            min_cost = Decimal("5")
        if position < min_cost:
            position = min_cost

        return position

    # ── İşlem Sonucu Kaydetme ────────────────────────────────────────────

    def record_trade(self, result: str, pnl: Decimal):
        """
        İşlem sonucunu kaydet ve state güncelle.

        Parametreler:
            result: "WIN" veya "LOSS"
            pnl:    Kâr/zarar miktarı (USDC). Kayıp ise negatif.
        """
        self._daily_pnl += pnl
        self._total_trades += 1

        if result == "WIN":
            self._wins += 1
            self._consecutive_wins += 1
            self._consecutive_losses = 0
            self._cooldown_until_window = 0

            # En iyi seri takibi
            if self._consecutive_wins > self._max_consecutive_wins:
                self._max_consecutive_wins = self._consecutive_wins

        elif result == "LOSS":
            self._losses += 1
            self._consecutive_losses += 1
            self._consecutive_wins = 0

            # En kötü seri takibi
            if self._consecutive_losses > self._max_consecutive_losses:
                self._max_consecutive_losses = self._consecutive_losses

            # Cooldown eşiğine ulaştıysa, cooldown süresini ayarla
            if self._consecutive_losses >= self._cfg.COOLDOWN_LOSSES:
                now = int(time.time())
                current_window = now - (now % 300)
                self._cooldown_until_window = (
                    current_window + (self._cfg.COOLDOWN_WINDOWS * 300)
                )

    # ── Pencere Sayacı (dry-run raporu için) ─────────────────────────────

    def record_window(self, traded: bool):
        """Her pencere sonunda çağrılır. Dry-run raporu için istatistik toplar."""
        self._total_windows += 1
        if not traded:
            self._total_skips += 1

    # ── Günlük Sıfırlama ────────────────────────────────────────────────

    def reset_daily(self):
        """Günlük sayaçları sıfırla. UTC 00:00'da çağrılır."""
        self._daily_pnl = Decimal("0")
        self._total_trades = 0
        self._wins = 0
        self._losses = 0
        # Art arda kayıp sıfırlanMAZ — gün değişse bile cooldown devam eder
        self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _check_daily_reset(self):
        """Gün değiştiyse otomatik sıfırla."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            self.reset_daily()

    # ── İstatistikler ────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """
        Günlük istatistikleri döndür.

        Dönüş dict'i hem günlük hem de toplam verileri içerir.
        """
        win_rate = Decimal("0")
        if self._total_trades > 0:
            win_rate = (
                Decimal(self._wins) / Decimal(self._total_trades) * Decimal("100")
            ).quantize(Decimal("0.1"))

        is_cooldown = (
            self._consecutive_losses >= self._cfg.COOLDOWN_LOSSES
            and self._cooldown_until_window > 0
        )

        return {
            "daily_pnl": self._daily_pnl,
            "total_trades": self._total_trades,
            "wins": self._wins,
            "losses": self._losses,
            "win_rate": win_rate,
            "consecutive_losses": self._consecutive_losses,
            "consecutive_wins": self._consecutive_wins,
            "is_cooldown": is_cooldown,
            # Dry-run raporu için ek alanlar
            "total_windows": self._total_windows,
            "total_skips": self._total_skips,
            "max_consecutive_losses": self._max_consecutive_losses,
            "max_consecutive_wins": self._max_consecutive_wins,
            "start_time": self._start_time,
        }

    def get_dry_run_verdict(self) -> str:
        """
        Win rate'e göre dry-run değerlendirmesi döndürür.

        Dönüş:
            "TEHLIKELI"  → %55 altı
            "AYARLA"     → %55-60
            "BASLA"      → %60-65
            "ARTIR"      → %65+
        """
        if self._total_trades == 0:
            return "YETERSIZ VERI"

        wr = float(self._wins) / float(self._total_trades) * 100

        if wr < 55:
            return "TEHLIKELI — Gercek parayla calistirma! Parametreleri gozden gecir."
        elif wr < 60:
            return "DIKKATLI — Parametreleri ayarla (threshold, timing). Kucuk basla."
        elif wr < 65:
            return "UYGUN — Kucuk miktarla ($50) gercek trading baslayabilirsin."
        else:
            return "GUZEL — Pozisyon boyutunu artirabilirsin."
