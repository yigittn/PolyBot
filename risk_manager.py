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

import json
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
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
            if token_price >= self._cfg.MAX_TOKEN_PRICE:
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

    # ── Kelly Criterion Pozisyon Boyutu ─────────────────────────────────

    def estimate_win_probability(self, token_price: Decimal) -> Decimal:
        """
        Token fiyatına göre gerçekçi kazanma olasılığı tahmini.

        Tarihsel verilere dayalı kalibrasyon (muhafazakar):
          $0.25-0.35 → p=0.52 (ucuz ama belirsiz)
          $0.35-0.44 → p=0.53 (en iyi bracket — ucuz token + conviction)
          $0.44+     → p=0.51 (edge çok düşük)

        Not: Ardışık kazanç bonusu kaldırıldı (gambler's fallacy).
        """
        tp = float(token_price)
        if tp <= 0.35:
            p = Decimal("0.52")
        elif tp <= 0.44:
            p = Decimal("0.53")
        else:
            p = Decimal("0.51")

        return min(p, Decimal("0.65"))

    def calculate_kelly_size(
        self,
        balance_usdc: Decimal,
        token_price: Decimal,
        confidence: int,
        book_size: Decimal = None,
    ) -> tuple[Decimal, dict]:
        """
        Binary Kelly ile pozisyon boyutu.

        f* = KELLY_FRACTION * (p - X) / (1 - X)

        Dönüş: (position_usdc, kelly_info)
          kelly_info = {p, f_star, f_adj, kelly_bet, reason}
          position_usdc = 0 → Kelly says skip (no edge)
        """
        info = {
            "p": Decimal("0"),
            "f_star": Decimal("0"),
            "f_adj": Decimal("0"),
            "kelly_bet": Decimal("0"),
            "reason": "",
        }

        if token_price <= 0 or token_price >= 1:
            info["reason"] = "Token fiyat gecersiz"
            return Decimal("0"), info

        p = self.estimate_win_probability(token_price)
        info["p"] = p

        # Binary Kelly: f* = (p - X) / (1 - X)
        f_star = (p - token_price) / (Decimal("1") - token_price)
        info["f_star"] = f_star

        if f_star <= Decimal("0.005"):
            info["reason"] = (
                f"Kelly edge yok: p={p}, X=${token_price}, f*={f_star:.4f}"
            )
            return Decimal("0"), info

        # Fractional Kelly (quarter Kelly default)
        f_adj = f_star * self._cfg.KELLY_FRACTION
        max_pct = self._cfg.KELLY_MAX_BET_PCT
        if f_adj > max_pct:
            f_adj = max_pct
        info["f_adj"] = f_adj

        kelly_bet = (balance_usdc * f_adj).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        info["kelly_bet"] = kelly_bet

        # Likidite kontrolü
        if book_size is not None and token_price > 0:
            available_usdc = book_size * token_price
            max_from_book = (available_usdc * Decimal("0.8")).quantize(Decimal("0.01"))
            if kelly_bet > max_from_book:
                kelly_bet = max_from_book

        # Minimum emir: 5 share × limit fiyatı
        limit_px = min(
            (token_price * Decimal("1.40")).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            ),
            self._cfg.MAX_TOKEN_PRICE,
        )
        min_cost = Decimal("5") * limit_px

        if kelly_bet < min_cost:
            if balance_usdc >= min_cost and f_star > Decimal("0.02"):
                kelly_bet = min_cost
                info["reason"] = "Kelly < min, minimum emir kullanildi"
            else:
                info["reason"] = (
                    f"Kelly bet ${kelly_bet} < min ${min_cost}, "
                    f"edge yetersiz (f*={f_star:.3f})"
                )
                return Decimal("0"), info

        if kelly_bet > balance_usdc:
            kelly_bet = balance_usdc

        if not info["reason"]:
            info["reason"] = "OK"

        return kelly_bet, info

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

    # ── State Kalıcılığı ─────────────────────────────────────────────────

    def save_state(self, path: str):
        """Risk state'ini JSON dosyasına yaz (crash sonrası kurtarma için)."""
        data = {
            "consecutive_losses": self._consecutive_losses,
            "consecutive_wins": self._consecutive_wins,
            "cooldown_until_window": self._cooldown_until_window,
            "daily_pnl": str(self._daily_pnl),
            "wins": self._wins,
            "losses": self._losses,
            "total_trades": self._total_trades,
            "current_date": self._current_date,
            "saved_at": time.time(),
        }
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"  [RiskManager] State kaydi basarisiz: {e}")

    def load_state(self, path: str):
        """Önceki oturumun risk state'ini yükle (yalnızca güncel tarihse)."""
        try:
            p = Path(path)
            if not p.exists():
                return
            data = json.loads(p.read_text())
            # Yalnızca aynı UTC günüyse art arda kayıp/cooldown devam etsin
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("current_date") != today:
                print(
                    f"  [RiskManager] State eskimiş ({data.get('current_date')}) — "
                    f"yeni gün, günlük sayaçlar sıfırlandı"
                )
                # Cooldown/consecutive bilgilerini koru (gün değişse de geçerli)
                self._consecutive_losses = data.get("consecutive_losses", 0)
                self._consecutive_wins = data.get("consecutive_wins", 0)
                self._cooldown_until_window = data.get("cooldown_until_window", 0)
                return
            self._consecutive_losses = data.get("consecutive_losses", 0)
            self._consecutive_wins = data.get("consecutive_wins", 0)
            self._cooldown_until_window = data.get("cooldown_until_window", 0)
            self._daily_pnl = Decimal(data.get("daily_pnl", "0"))
            self._wins = data.get("wins", 0)
            self._losses = data.get("losses", 0)
            self._total_trades = data.get("total_trades", 0)
            self._current_date = data.get("current_date", today)
            print(
                f"  [RiskManager] State yuklendi: {self._consecutive_losses} ardisik kayip, "
                f"cooldown_until={self._cooldown_until_window}, "
                f"daily_pnl={self._daily_pnl}"
            )
        except Exception as e:
            print(f"  [RiskManager] State yuklenemedi: {e}")

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
