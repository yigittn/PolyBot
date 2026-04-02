"""
auto_claimer.py — Otomatik Kazanç Toplayıcı
=============================================
Production: Bakiye değişimi + ertelenmiş uzlaştırma (deferred reconciliation).

Polymarket redeem'leri bazen dakikalar, bazen saatler sürer.
Bu yüzden 2 aşamalı tespit:
  1. Hızlı kontrol (120s polling): WIN hemen tespit edilir
  2. Ertelenmiş uzlaştırma: Her window'da bakiye-maliyet karşılaştırması,
     geç gelen redeem'ler PENDING → WIN'e çevrilir.
  3. Zaman aşımı: 20 pencere (100dk) sonra hâlâ PENDING → LOSS kabul edilir.

Dry-run: Exchange fiyat karşılaştırması (heuristic).
"""

import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

from config import cfg


class AutoClaimer:
    """Kazanan pozisyonları tespit eder (hızlı + ertelenmiş)."""

    QUICK_POLL_INTERVAL = 30
    QUICK_POLL_MAX = 120

    def __init__(self, config=None):
        self._cfg = config or cfg
        self._pending_trades: dict[int, dict] = {}
        self._pending_reconciliation: list[dict] = []
        self._total_costs = Decimal("0")
        self._confirmed_payouts = Decimal("0")
        self._load_pending_from_log()

    def _load_pending_from_log(self):
        """Startup: trades.jsonl'den PENDING trade'leri yükle."""
        log_path = Path(self._cfg.PROJECT_ROOT) / self._cfg.LOG_FILE
        if not log_path.exists():
            return

        trades_by_window: dict[int, dict] = {}
        results_by_window: dict[int, dict] = {}

        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    wts = entry.get("window_ts", 0)
                    event = entry.get("event", "")
                    if event == "TRADE":
                        trades_by_window[wts] = entry
                    elif event == "RESULT":
                        results_by_window[wts] = entry
        except Exception as e:
            print(f"  [AutoClaimer] trades.jsonl okuma hatasi: {e}")
            return

        loaded = 0
        for wts, result in results_by_window.items():
            if result.get("result") != "PENDING":
                continue
            trade = trades_by_window.get(wts)
            if not trade:
                continue

            self._pending_reconciliation.append({
                "window_ts": wts,
                "direction": trade.get("direction", ""),
                "shares": Decimal(str(trade.get("shares", "0"))),
                "cost_usdc": Decimal(str(trade.get("total_cost_usdc", "0"))),
                "token_id": trade.get("token_id", trade.get("order_id", "")),
                "created_at": time.time() - 600,
            })
            self._total_costs += Decimal(str(trade.get("total_cost_usdc", "0")))
            loaded += 1

        if loaded:
            print(f"  [AutoClaimer] {loaded} PENDING trade trades.jsonl'den yuklendi")

    def register_trade(self, window_ts: int, trade_info: dict):
        """Yeni işlemi bekleyen listesine ekle."""
        self._pending_trades[window_ts] = trade_info
        self._total_costs += trade_info.get("cost_usdc", Decimal("0"))

    async def check_and_claim(
        self,
        executor,
        window_ts: int = 0,
        exchange_feed=None,
        oracle_monitor=None,
    ) -> list:
        results = []

        trade = self._pending_trades.pop(window_ts, None)
        if not trade:
            return results

        token_id = trade.get("token_id", "")
        direction = trade.get("direction", "")
        cost_usdc = trade.get("cost_usdc", Decimal("0"))
        shares = trade.get("shares", Decimal("0"))

        try:
            if self._cfg.DRY_RUN:
                result = self._determine_outcome_by_price(
                    direction, shares, token_id, window_ts,
                    exchange_feed, oracle_monitor,
                )
            else:
                result = await self._quick_balance_check(
                    executor, direction, shares, token_id,
                    cost_usdc, window_ts,
                )
            result["cost_usdc"] = cost_usdc
            results.append(result)

        except Exception as e:
            results.append({
                "window_ts": window_ts,
                "token_id": token_id,
                "direction": direction,
                "result": "UNKNOWN",
                "cost_usdc": cost_usdc,
                "payout": Decimal("0"),
                "claimed": False,
                "error": f"Sonuc kontrolu hatasi: {str(e)}",
            })

        cutoff = window_ts - (5 * 300)
        stale_keys = [k for k in self._pending_trades if k < cutoff]
        for k in stale_keys:
            del self._pending_trades[k]

        return results

    # ── Production: Hızlı bakiye kontrolü (120s) ─────────────────────

    async def _quick_balance_check(
        self,
        executor,
        direction: str,
        shares: Decimal,
        token_id: str,
        cost_usdc: Decimal,
        window_ts: int,
    ) -> dict:
        """
        120 saniye boyunca bakiye değişimini kontrol et.
        WIN tespit edilirse hemen dön.
        Yoksa PENDING olarak işaretle (ertelenmiş uzlaştırma bekle).
        """
        base_result = {
            "window_ts": window_ts,
            "token_id": token_id,
            "direction": direction,
            "result": "UNKNOWN",
            "payout": Decimal("0"),
            "claimed": False,
            "error": None,
        }

        balance_before = await executor.get_balance()
        threshold = shares * Decimal("0.5")

        print(
            f"  [AutoClaimer] Hizli kontrol basladi "
            f"(her {self.QUICK_POLL_INTERVAL}s, max {self.QUICK_POLL_MAX}s)... "
            f"Bakiye: ${balance_before}"
        )

        waited = 0
        balance_after = balance_before
        while waited < self.QUICK_POLL_MAX:
            await asyncio.sleep(self.QUICK_POLL_INTERVAL)
            waited += self.QUICK_POLL_INTERVAL
            balance_after = await executor.get_balance()
            delta = balance_after - balance_before

            if delta >= threshold:
                print(
                    f"  [AutoClaimer] WIN tespit ({waited}s) — "
                    f"${balance_before} → ${balance_after} (Δ${delta:+})"
                )
                self._confirmed_payouts += delta
                base_result["result"] = "WIN"
                base_result["payout"] = delta
                base_result["claimed"] = True
                return base_result

            print(
                f"  [AutoClaimer] Bekleniyor ({waited}/{self.QUICK_POLL_MAX}s) — "
                f"Δ${delta:+}"
            )

        print(
            f"  [AutoClaimer] Henuz sonuc yok — PENDING olarak kaydediliyor. "
            f"Gec gelen redeem ertelenmis uzlastirmada yakalanacak."
        )
        self._pending_reconciliation.append({
            "window_ts": window_ts,
            "direction": direction,
            "shares": shares,
            "cost_usdc": cost_usdc,
            "token_id": token_id,
            "created_at": time.time(),
        })
        base_result["result"] = "PENDING"
        return base_result

    # ── Ertelenmiş uzlaştırma: Geç gelen redeem'leri yakala ─────────

    async def reconcile_pending(self, executor, initial_balance: Decimal) -> list:
        """
        Bekleyen PENDING trade'leri bakiye karşılaştırmasıyla çöz.

        Her window başında çağrılır. Mantık:
          expected = initial - total_costs + confirmed_payouts
          actual   = get_balance()
          surplus  = actual - expected
          surplus >= shares*0.5 → en eski PENDING trade WIN
          20+ pencere (100dk) geçtiyse → LOSS kabul et, temizle
        """
        if not self._pending_reconciliation:
            return []

        actual = await executor.get_balance()
        expected = initial_balance - self._total_costs + self._confirmed_payouts
        surplus = actual - expected

        results = []
        now = time.time()
        pending_timeout_seconds = 20 * 300  # 20 pencere = 6000s = 100dk

        still_pending = []
        for trade in self._pending_reconciliation:
            payout_threshold = trade["shares"] * Decimal("0.5")
            created = trade.get("created_at")
            if created is None:
                created = now - pending_timeout_seconds
                trade["created_at"] = created
            age_seconds = now - created

            if surplus >= payout_threshold:
                payout = trade["shares"]
                self._confirmed_payouts += payout
                surplus -= payout
                print(
                    f"  [Reconcile] PENDING → WIN! Window {trade['window_ts']} "
                    f"({trade['direction']}) | Payout ~${payout}"
                )
                results.append({
                    "window_ts": trade["window_ts"],
                    "token_id": trade["token_id"],
                    "direction": trade["direction"],
                    "result": "WIN",
                    "cost_usdc": trade["cost_usdc"],
                    "payout": payout,
                    "claimed": True,
                    "error": None,
                    "reconciled": True,
                })
            elif age_seconds >= pending_timeout_seconds:
                print(
                    f"  [Reconcile] PENDING → LOSS (timeout {age_seconds/60:.0f}dk) "
                    f"Window {trade['window_ts']} ({trade['direction']}) | "
                    f"Cost: ${trade['cost_usdc']}"
                )
                results.append({
                    "window_ts": trade["window_ts"],
                    "token_id": trade["token_id"],
                    "direction": trade["direction"],
                    "result": "LOSS",
                    "cost_usdc": trade["cost_usdc"],
                    "payout": Decimal("0"),
                    "claimed": False,
                    "error": None,
                    "reconciled": True,
                })
            else:
                still_pending.append(trade)

        self._pending_reconciliation = still_pending

        if self._pending_reconciliation:
            oldest_age = (now - self._pending_reconciliation[0].get("created_at", now)) / 60
            print(
                f"  [Reconcile] {len(self._pending_reconciliation)} trade hala PENDING "
                f"(surplus: ${surplus:.2f}, en eski: {oldest_age:.0f}dk)"
            )

        return results

    @property
    def pending_count(self) -> int:
        return len(self._pending_reconciliation)

    @property
    def pending_win_value(self) -> Decimal:
        """Kullanılmıyor — geriye uyumluluk için sıfır döner."""
        return Decimal("0")

    # ── Dry-run: Fiyat karşılaştırmasıyla sonuç belirle ──────────────

    def _determine_outcome_by_price(
        self,
        direction: str,
        shares: Decimal,
        token_id: str,
        window_ts: int,
        exchange_feed,
        oracle_monitor,
    ) -> dict:
        base_result = {
            "window_ts": window_ts,
            "token_id": "DRY-RUN",
            "direction": direction,
            "result": "UNKNOWN",
            "payout": Decimal("0"),
            "claimed": False,
            "error": None,
        }

        if not exchange_feed or not oracle_monitor:
            base_result["error"] = "Exchange/oracle verisi yok"
            return base_result

        open_price = oracle_monitor.get_window_open_price(window_ts)
        if not open_price or open_price <= 0:
            base_result["error"] = "Pencere acilis fiyati yok"
            return base_result

        price_data = exchange_feed.get_price()
        close_price = price_data.get("average")
        if not close_price:
            od = oracle_monitor.get_oracle_data()
            op = od.get("price")
            if op is not None and op > 0:
                close_price = op
        if not close_price:
            base_result["error"] = "Exchange ve oracle kapanish fiyati yok"
            return base_result

        actual_direction = "UP" if close_price > open_price else "DOWN"

        if direction == actual_direction:
            base_result["result"] = "WIN"
            base_result["payout"] = shares
        else:
            base_result["result"] = "LOSS"
            base_result["payout"] = Decimal("0")

        return base_result
