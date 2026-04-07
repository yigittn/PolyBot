"""
signal_engine.py — Sinyal Üretici
====================================
Exchange fiyatı, oracle fiyatı ve pencere açılış fiyatından
UP / DOWN / SKIP sinyali ve güvenilirlik skoru (confidence) üretir.

Sinyal mantığı (v2 — Oracle Onaylı):
  1. Exchange delta > THRESHOLD  (yönü belirle)
  2. Oracle delta da aynı yönde  (onay — Polymarket'in resolve kaynağı)
  3. Yeterli volatilite var       (gürültü değil gerçek hareket)
  → Tümü uyarsa: UP/DOWN sinyali. Aksi: SKIP.

Kullanım:
    from signal_engine import SignalEngine
    engine = SignalEngine(cfg)
    signal = engine.generate_signal(exchange_data, oracle_data, window_open_price)
"""

from decimal import Decimal
from typing import Optional

from config import cfg


class SignalEngine:
    """Fiyat delta'sından UP/DOWN/SKIP sinyali ve confidence skoru üretir."""

    def __init__(self, config=None):
        self._cfg = config or cfg

    def generate_signal(
        self,
        exchange_data: dict,
        oracle_data: dict,
        window_open_price: Decimal,
        up_best_ask: Optional[Decimal] = None,
        down_best_ask: Optional[Decimal] = None,
    ) -> dict:
        result = {
            "direction": "SKIP",
            "delta_percent": Decimal("0"),
            "oracle_lag": 0,
            "confidence": 0,
            "reason": "",
        }

        # ── Veri doğrulama ──────────────────────────────────────────────
        exchange_avg = exchange_data.get("average")
        if exchange_avg is None:
            result["reason"] = "SKIP: exchange verisi yok"
            return result

        oracle_price = oracle_data.get("price")
        if oracle_price is None:
            result["reason"] = "SKIP: oracle verisi yok"
            return result

        if window_open_price is None or window_open_price == 0:
            result["reason"] = "SKIP: pencere acilis fiyati yok"
            return result

        oracle_lag = oracle_data.get("lag_seconds", 0)
        result["oracle_lag"] = oracle_lag

        # ── SKIP: Exchange verisi bayat mı? ─────────────────────────────
        if exchange_data.get("is_stale", True):
            result["reason"] = "SKIP: exchange verisi bayat (30s+ guncelleme yok)"
            return result

        # ── SKIP: Exchange divergence çok yüksek mi? ────────────────────
        divergence = exchange_data.get("divergence_pct", Decimal("0"))
        if divergence > self._cfg.EXCHANGE_DIVERGENCE_MAX:
            result["reason"] = (
                f"SKIP: exchange divergence {divergence}% > "
                f"max {self._cfg.EXCHANGE_DIVERGENCE_MAX}%"
            )
            return result

        # ── SKIP: Oracle çok taze mi? ───────────────────────────────────
        if oracle_lag < self._cfg.ORACLE_LAG_FRESH:
            result["reason"] = (
                f"SKIP: oracle {oracle_lag}s once guncellendi, avantaj yok "
                f"(esik: {self._cfg.ORACLE_LAG_FRESH}s)"
            )
            return result

        # ── Delta hesapla (exchange-based, direction signal) ────────────
        exchange_delta = (exchange_avg - window_open_price) / window_open_price * Decimal("100")
        result["delta_percent"] = exchange_delta

        abs_delta = abs(exchange_delta)
        if abs_delta < self._cfg.DELTA_THRESHOLD:
            result["reason"] = (
                f"SKIP: delta {exchange_delta}% < threshold "
                f"{self._cfg.DELTA_THRESHOLD}%"
            )
            return result

        # ── SKIP: Oracle lag yeterli mi? ────────────────────────────────
        if oracle_lag < self._cfg.ORACLE_LAG_MIN:
            result["reason"] = (
                f"SKIP: oracle lag {oracle_lag}s < min {self._cfg.ORACLE_LAG_MIN}s"
            )
            return result

        # ── Oracle onay: Polymarket oracle'ı da aynı yönde mi? ─────────
        oracle_delta = (oracle_price - window_open_price) / window_open_price * Decimal("100")

        exchange_up = exchange_delta > 0
        oracle_up = oracle_delta > 0

        if exchange_up != oracle_up and abs(oracle_delta) > Decimal("0.01"):
            result["reason"] = (
                f"SKIP: oracle onay yok — exchange {'+' if exchange_up else '-'}"
                f" vs oracle {'+' if oracle_up else '-'} "
                f"(ex: {exchange_delta:.3f}%, orc: {oracle_delta:.3f}%)"
            )
            return result

        # ── Volatilite filtresi: 15dk rolling range < %0.10 → yatay piyasa ─
        rolling_range = exchange_data.get("rolling_range_pct")
        if rolling_range is not None and rolling_range < Decimal("0.10"):
            result["reason"] = (
                f"SKIP: Dusuk volatilite - son 15dk range "
                f"%{rolling_range:.2f} < %0.10"
            )
            return result

        # ── Trend filtresi: 15dk yönsel hareket > %0.7 → trend piyasa ──
        trend_pct = exchange_data.get("trend_pct")
        if trend_pct is not None and abs(trend_pct) > Decimal("0.70"):
            result["reason"] = (
                f"SKIP: Trend piyasa - son 15dk hareket "
                f"%{trend_pct:+.3f} (esik: ±%0.70)"
            )
            return result

        # ── Yön belirle ────────────────────────────────────────────────
        direction = "UP" if exchange_delta > 0 else "DOWN"

        # ── Seçilen tarafta token fiyatı üst sınırı (MAX_TOKEN_PRICE) ──
        chosen_ask = (
            up_best_ask if direction == "UP" else down_best_ask
        )
        if chosen_ask is not None and chosen_ask >= self._cfg.MAX_TOKEN_PRICE:
            result["direction"] = "SKIP"
            result["reason"] = (
                f"SKIP: {direction} token ${chosen_ask} >= max "
                f"${self._cfg.MAX_TOKEN_PRICE}"
            )
            return result

        # ── Confidence skoru hesapla (0-100) ───────────────────────────
        confidence = self._calculate_confidence(
            abs_delta, oracle_lag, exchange_data, oracle_delta,
        )

        # ── SKIP: Confidence yeterli mi? ───────────────────────────────
        if confidence < 50:
            result["direction"] = "SKIP"
            result["confidence"] = confidence
            result["reason"] = (
                f"SKIP: confidence {confidence} < 50 "
                f"(delta: {exchange_delta:.4f}%, lag: {oracle_lag}s)"
            )
            return result

        # ── Sinyal geçerli! ────────────────────────────────────────────
        result["direction"] = direction
        result["confidence"] = confidence
        result["reason"] = (
            f"Delta {'+' if exchange_delta > 0 else ''}{exchange_delta:.4f}% "
            f"| Oracle {'+' if oracle_delta > 0 else ''}{oracle_delta:.4f}% "
            f"| Lag {oracle_lag}s | Conf {confidence}"
        )
        return result

    def _calculate_confidence(
        self,
        abs_delta: Decimal,
        oracle_lag: int,
        exchange_data: dict,
        oracle_delta: Decimal,
    ) -> int:
        """
        Güvenilirlik skoru (0-100).

        Bileşenler:
          - Delta büyüklüğü:      max 35 puan
          - Oracle lag:            max 25 puan
          - Exchange uyumu:        max 15 puan
          - Oracle onay gücü:     max 25 puan  (YENİ — en kritik faktör)
        """
        confidence = 0

        # ── Delta büyüklüğü (max 35 puan) ─────────────────────────────
        if abs_delta >= Decimal("0.15"):
            confidence += 35
        elif abs_delta >= Decimal("0.10"):
            confidence += 28
        elif abs_delta >= Decimal("0.07"):
            confidence += 20
        elif abs_delta >= Decimal("0.05"):
            confidence += 12

        # ── Oracle lag (max 25 puan) ───────────────────────────────────
        if oracle_lag >= 30:
            confidence += 25
        elif oracle_lag >= 20:
            confidence += 20
        elif oracle_lag >= 10:
            confidence += 15
        elif oracle_lag >= 8:
            confidence += 8

        # ── Exchange uyumu (max 15 puan) ───────────────────────────────
        divergence = exchange_data.get("divergence_pct", Decimal("99"))
        is_reliable = exchange_data.get("is_reliable", False)

        if is_reliable and divergence < Decimal("0.05"):
            confidence += 15
        elif divergence < Decimal("0.10"):
            confidence += 8

        # ── Oracle onay gücü (max 25 puan) ─────────────────────────────
        abs_oracle = abs(oracle_delta)
        if abs_oracle >= Decimal("0.08"):
            confidence += 25
        elif abs_oracle >= Decimal("0.05"):
            confidence += 18
        elif abs_oracle >= Decimal("0.03"):
            confidence += 10
        elif abs_oracle >= Decimal("0.02"):
            confidence += 5

        return confidence
