"""
run.py — Ana Giriş Noktası (Orkestratör)
==========================================
Tüm modülleri bir araya getirir ve ana döngüyü çalıştırır.

Kullanım:
    python -u run.py           # -u önerilir (Cursor/IDE terminalinde çıktı hemen görünsün)
    python run.py              # .env'deki DRY_RUN ayarına göre çalışır
    python run.py --dry-run    # Her zaman simülasyon (emir gönderilmez)
    python run.py --claim-only # Sadece bekleyen kazançları topla

Ana döngü her 5 dakikalık pencere için:
    1. Pencere açılışında oracle fiyatını cache'le
    2. T-30s → T-10s arası: sinyal üret, uygunsa emir gönder
    3. T-0s: sonucu kontrol et, claim yap
"""

import asyncio
import atexit
import os
import signal
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from pathlib import Path

from config import cfg
from exchange_feed import ExchangeFeed
from oracle_monitor import OracleMonitor
from market_resolver import MarketResolver
from signal_engine import SignalEngine
from risk_manager import RiskManager
from order_executor import OrderExecutor
from auto_claimer import AutoClaimer
from logger_module import (
    log_trade, log_result, log_skip, log_cooldown,
    log_error, print_summary, print_dry_run_report,
    _now_str, _C,
)


class Bot:
    """Tüm modülleri orkestra eden ana bot sınıfı."""

    def __init__(self):
        self.exchange = ExchangeFeed(cfg)
        self.oracle = OracleMonitor(cfg)
        self.resolver = MarketResolver(cfg)
        self.signal = SignalEngine(cfg)
        self.risk = RiskManager(cfg)
        self.executor = OrderExecutor(cfg)
        self.claimer = AutoClaimer(cfg)

        self._risk_state_path = str(
            Path(cfg.PROJECT_ROOT) / "data" / "risk_state.json"
        )
        self._traded_this_window: int = 0
        self._order_attempted_this_window: set[str] = set()
        self._try_trade_lock = asyncio.Lock()
        self._running = True

        # ── Production state ──────────────────────────────────────────
        self._production_trade_count: int = 0
        self._consecutive_order_rejects: int = 0
        self._initial_balance: Decimal = Decimal("0")
        self._window_count: int = 0
        self._stop_reason: str = ""
        self._settled_window_closes: set[int] = set()
        self._committed_trade_window: int = 0
        # Aynı pencere için çift sonuç logunu önle (sınırda tekrar tetiklenme vb.)

    # ══════════════════════════════════════════════════════════════════
    #  BAŞLATMA
    # ══════════════════════════════════════════════════════════════════

    async def start(self):
        """Tüm modülleri başlat ve ana döngüyü çalıştır."""

        if not cfg.DRY_RUN:
            passed = await self._production_preflight()
            if not passed:
                await self.shutdown()
                return
        else:
            cfg.print_summary()
            await self._start_modules()

        try:
            await self._main_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def _start_modules(self):
        """Tüm modülleri başlat (ortak başlatma)."""
        assets_str = ",".join(cfg.TRADING_ASSETS)
        print(f"  Moduller baslatiliyor... (varliklar: {assets_str})")

        print(f"  [1/4] Exchange feed ({assets_str} — Binance + Coinbase + Bitstamp)...")
        await self.exchange.start()

        print(f"  [2/4] Chainlink oracle monitor ({assets_str})...")
        await self.oracle.start()

        print("  [3/4] Polymarket CLOB client...")
        await self.resolver.initialize()
        await self.executor.initialize()

        print("  [4/4] Bakiye kontrol ediliyor...")
        balance = await self.executor.get_balance()
        self._initial_balance = balance
        print(f"  Bakiye: ${balance} USDC")

    # ══════════════════════════════════════════════════════════════════
    #  PRODUCTION PRE-FLIGHT (8 nokta kontrol)
    # ══════════════════════════════════════════════════════════════════

    async def _production_preflight(self) -> bool:
        """Production başlatma öncesi 8 noktalı kontrol listesi."""
        sep = "=" * 50
        print(f"\n  {sep}")
        print(f"  PRODUCTION BASLANGIC KONTROLLERI")
        print(f"  {sep}\n")

        # 1. DRY_RUN=false onay
        print("  [1/8] DRY_RUN=false kontrol...")
        if cfg.DRY_RUN:
            print("  BASARISIZ: DRY_RUN hala true!")
            return False
        print(f"        GECTI — GERCEK ISLEM MODU")

        # 2. Modülleri başlat (bakiye dahil)
        try:
            await self._start_modules()
        except Exception as e:
            print(f"\n  BASARISIZ: Modul baslatilamadi: {e}")
            return False

        # Risk state yükle (crash sonrası art arda kayıp/cooldown kaybetme)
        self.risk.load_state(self._risk_state_path)

        # 3. Bakiye kontrolü
        print(f"\n  [2/8] USDC bakiye kontrol...")
        if self._initial_balance < Decimal("2.5"):
            print(
                f"        BASARISIZ — Bakiye ${self._initial_balance} < $2.50 minimum.\n"
                f"        Polymarket cüzdanına USDC yatır."
            )
            return False
        print(f"        GECTI — ${self._initial_balance} USDC")

        # 4. Binance WebSocket (tüm asset'ler) — US bölgelerinde .us'a geçiş yapılır
        print("  [3/8] Binance WebSocket (binance.com → binance.us otomatik)...")
        await asyncio.sleep(5)  # .us geçişi için biraz daha bekle
        for asset in cfg.TRADING_ASSETS:
            ex = self.exchange.get_price(asset)
            if ex.get("binance") is None:
                print(
                    f"        UYARI — {asset} Binance verisi henuz yok "
                    f"(binance.us'a gecis devam ediyor, Coinbase ile devam)"
                )
            else:
                print(f"        GECTI — {asset} ${ex['binance']:.2f}")

        # 5. Coinbase WebSocket (zorunlu — Binance yoksa tek kaynak)
        print("  [4/8] Coinbase WebSocket...")
        for asset in cfg.TRADING_ASSETS:
            ex = self.exchange.get_price(asset)
            if ex.get("coinbase") is None:
                # Binance da yoksa gerçekten veri yok — durdur
                if ex.get("binance") is None:
                    print(f"        BASARISIZ — {asset} Coinbase VE Binance verisi yok!")
                    return False
                print(f"        UYARI — {asset} Coinbase verisi yok, sadece Binance ile devam")
            else:
                print(f"        GECTI — {asset} ${ex['coinbase']:.2f}")

        # 6. Oracle (tüm asset'ler)
        print("  [5/8] Chainlink oracle...")
        for asset in cfg.TRADING_ASSETS:
            orc = self.oracle.get_oracle_data(asset)
            if orc.get("price") is None:
                print(f"        BASARISIZ — {asset} Oracle fiyat verisi yok!")
                return False
            print(f"        GECTI — {asset} ${orc['price']:.2f} (lag {orc['lag_seconds']}s)")

        # 7. CLOB API
        print("  [6/8] Polymarket CLOB API...")
        try:
            test_balance = await self.executor.get_balance()
            if test_balance >= 0:
                print(f"        GECTI — API yanit veriyor")
            else:
                print("        BASARISIZ — CLOB API yanit vermiyor")
                return False
        except Exception as e:
            print(f"        BASARISIZ — {e}")
            return False

        # 8. Aktif piyasa (tüm asset'ler)
        print("  [7/8] Aktif 5-dk piyasa kontrolu...")
        for asset in cfg.TRADING_ASSETS:
            market = await self.resolver.get_active_market(asset)
            if market:
                print(f"        GECTI — [{asset}] {market.get('slug', '?')}")
            else:
                print(f"        UYARI — [{asset}] Su an aktif piyasa yok")

        # 9. Tüm kontroller geçti
        print(f"  [8/8] Son kontroller...")
        print(f"        GECTI — Tum sistemler hazir\n")

        wallet = f"{cfg.FUNDER_ADDRESS[:8]}...{cfg.FUNDER_ADDRESS[-4:]}"
        assets_str = ",".join(cfg.TRADING_ASSETS)
        banner = f"""
  +================================================+
  |  POLYMARKET BOT — PRODUCTION MODE               |
  +================================================+
  |  Varliklar:     {assets_str:<30s} |
  |  Cuzdan:        {wallet:<30s} |
  |  Bakiye:        ${str(self._initial_balance):<29s} |
  |  Pozisyon:      ${str(cfg.MAX_POSITION):<28s} / islem |
  |  Gunluk limit:  -${str(cfg.DAILY_LOSS_LIMIT):<27s} |
  |  Acil durdurma: Bakiye < ${str(cfg.EMERGENCY_BALANCE_MIN):<20s} |
  |  Ilk {cfg.PROTECTION_TRADES} islem:  {cfg.PROTECTION_DELAY}s onay gecikmeli{'':<15s} |
  |                                                  |
  |  Durdurmak icin: Ctrl+C                          |
  +================================================+
"""
        print(banner)
        return True

    # ══════════════════════════════════════════════════════════════════
    #  ACİL DURDURMA MEKANİZMASI
    # ══════════════════════════════════════════════════════════════════

    async def _check_emergency_stop(self) -> bool:
        """
        6 acil durdurma koşulunu kontrol et.
        True döndürürse bot durmalı.
        """
        if cfg.DRY_RUN:
            return False

        # 1. Bakiye < EMERGENCY_BALANCE_MIN
        # API geçici hata verirse $0 döner — yanlış durdurmaları önlemek için
        # 3 kez dene, hepsi başarısızsa bu kontrolü atla (diğer korumalar çalışır)
        balance_ok = False
        for _attempt in range(3):
            try:
                balance = await self.executor.get_balance()
                if balance is not None and balance > 0:
                    balance_ok = True
                    if balance < cfg.EMERGENCY_BALANCE_MIN:
                        self._stop_reason = (
                            f"Bakiye ${balance} < ${cfg.EMERGENCY_BALANCE_MIN} minimum"
                        )
                        return True
                    break
            except Exception:
                await asyncio.sleep(2)

        # 2. Günlük kayıp > DAILY_LOSS_LIMIT
        stats = self.risk.get_stats()
        if stats["daily_pnl"] < -cfg.DAILY_LOSS_LIMIT:
            self._stop_reason = (
                f"Gunluk kayip ${stats['daily_pnl']} > "
                f"-${cfg.DAILY_LOSS_LIMIT} limit"
            )
            return True

        # 3. EMERGENCY_CONSECUTIVE_LOSSES ardışık kayıp
        if stats["consecutive_losses"] >= cfg.EMERGENCY_CONSECUTIVE_LOSSES:
            self._stop_reason = (
                f"{stats['consecutive_losses']} ardisik kayip — "
                f"piyasa kosullari uygun degil"
            )
            return True

        # 4. EMERGENCY_ORDER_REJECTS ardışık emir reddi
        if self._consecutive_order_rejects >= cfg.EMERGENCY_ORDER_REJECTS:
            self._stop_reason = (
                f"{self._consecutive_order_rejects} ardisik emir reddi — "
                f"API sorunu olabilir"
            )
            return True

        # 5. Exchange feed 60+ saniye stale (herhangi bir asset yeterli)
        any_exchange_ok = False
        for asset in cfg.TRADING_ASSETS:
            ex = self.exchange.get_price(asset)
            if not (ex.get("is_stale") and ex.get("average") is None):
                any_exchange_ok = True
                break
        if not any_exchange_ok:
            now = time.time()
            latest = max(
                max(self.exchange._binance_updated.values(), default=0),
                max(self.exchange._coinbase_updated.values(), default=0),
            )
            if latest > 0 and (now - latest) > 60:
                self._stop_reason = (
                    f"Exchange verisi {int(now - latest)}s dir gelmiyor"
                )
                return True

        # 6. Oracle 120+ saniye stale (tüm asset'ler stale ise durdur)
        all_oracle_stale = True
        for asset in cfg.TRADING_ASSETS:
            orc = self.oracle.get_oracle_data(asset)
            if not (orc["lag_seconds"] > 120 and orc.get("price") is not None):
                all_oracle_stale = False
                break
        if all_oracle_stale:
            orc = self.oracle.get_oracle_data(cfg.TRADING_ASSETS[0])
            self._stop_reason = (
                f"Oracle {orc['lag_seconds']}s dir guncellenmiyor"
            )
            return True

        return False

    def _print_emergency_stop(self):
        """Acil durdurma mesajını yazdır."""
        stats = self.risk.get_stats()
        balance = "?"

        msg = f"""
  {_C.RED}{_C.BOLD}
  ╔══════════════════════════════════════════════╗
  ║  ACIL DURDURMA                                ║
  ╠══════════════════════════════════════════════╣
  ║  Sebep: {self._stop_reason:<37s} ║
  ║  Bakiye:     ${str(balance):<32s} ║
  ║  Gunluk PnL: ${str(stats['daily_pnl']):<32s} ║
  ║  Islemler:   {stats['total_trades']} ({stats['wins']}W/{stats['losses']}L){'':<22s} ║
  ║                                                ║
  ║  Bot guvenli sekilde durduruldu.               ║
  ║  Tekrar baslatmak icin: python run.py          ║
  ╚══════════════════════════════════════════════╝
  {_C.RESET}"""
        print(msg)
        log_error(f"ACIL DURDURMA: {self._stop_reason}", {
            "daily_pnl": str(stats["daily_pnl"]),
            "trades": stats["total_trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
        })

    # ══════════════════════════════════════════════════════════════════
    #  ANA DÖNGÜ
    # ══════════════════════════════════════════════════════════════════

    async def _main_loop(self):
        """
        Sonsuz döngü — her 5 dakikalık pencereyi takip eder.

        Pencere yaşam döngüsü:
          kalan > 30s  : Veri topla, izle
          kalan = 300  : Pencere açılışı → oracle fiyatını cache'le
          30s > kalan > 10s : İŞLEM PENCERESİ → sinyal üret, emir gönder
          kalan < 10s  : Bekle, emir gönderme
          kalan = 0    : Sonuç kontrol, claim, istatistik
        """
        last_window_ts = 0

        print(f"\n  Bot baslatildi. Ctrl+C ile durdurabilirsin.\n")
        print("  " + "=" * 50)

        while self._running:
            try:
                # Acil durdurma kontrolü (her 10 pencerede + her trade sonrası)
                if self._window_count > 0 and self._window_count % 5 == 0:
                    if await self._check_emergency_stop():
                        self._print_emergency_stop()
                        self._running = False
                        return

                window = self.resolver.get_current_window()
                remaining = window["seconds_remaining"]
                window_ts = window["window_ts"]

                # ── Yeni pencere başlangıcı ──────────────────────────────
                if window_ts != last_window_ts:
                    if last_window_ts > 0:
                        # Açık GTC emirlerini iptal et (collateral kilitlemeyi önle)
                        if not cfg.DRY_RUN:
                            await self.executor.cancel_all_orders()

                        prev_traded = (self._traded_this_window == last_window_ts)
                        self.risk.record_window(prev_traded)
                        await self._handle_window_close(last_window_ts)

                    last_window_ts = window_ts
                    self._traded_this_window = 0
                    self._committed_trade_window = 0
                    self._order_attempted_this_window.clear()
                    self._window_count += 1

                    for asset in cfg.TRADING_ASSETS:
                        self.oracle.cache_window_open_price(window_ts, asset)

                    # Günlük reset kontrolü
                    self.risk._check_daily_reset()

                    print(
                        f"\n  --- Yeni pencere: {window_ts} | "
                        f"Kapanisa {remaining}s ---"
                    )

                    # Her 10 pencerede bakiye raporu
                    if not cfg.DRY_RUN and self._window_count % 10 == 0:
                        await self._print_balance_report()

                # ── Pencere sonuna uzak: izle ───────────────────────────
                if remaining > cfg.ENTRY_WINDOW_START:
                    parts = []
                    for asset in cfg.TRADING_ASSETS:
                        ex = self.exchange.get_price(asset)
                        orc = self.oracle.get_oracle_data(asset)
                        if ex.get("average") and orc.get("price"):
                            delta = "?"
                            wop = self.oracle.get_window_open_price(window_ts, asset)
                            if wop and wop > 0:
                                delta = ((ex["average"] - wop) / wop * Decimal("100")).quantize(Decimal("0.001"))
                            parts.append(
                                f"{asset} ${ex['average']:.2f} "
                                f"(orc {orc['lag_seconds']}s D:{delta}%)"
                            )
                    if parts:
                        print(
                            f"  [{remaining:>3d}s] " + " | ".join(parts),
                            end="\r",
                            flush=True,
                        )
                    await asyncio.sleep(10)
                    continue

                # ── İşlem penceresi: T-30s → T-10s ──────────────────────
                if window["is_entry_window"] and self._traded_this_window != window_ts:
                    await self._try_trade(window_ts, remaining)
                    await asyncio.sleep(2)
                    continue

                # ── Çok geç: T-10s → T-0s ───────────────────────────────
                if remaining <= cfg.ENTRY_WINDOW_END:
                    await asyncio.sleep(1)
                    continue

                await asyncio.sleep(2)

            except Exception as e:
                log_error(f"Ana dongu hatasi: {e}")
                await asyncio.sleep(5)

    # ══════════════════════════════════════════════════════════════════
    #  BAKİYE RAPORU
    # ══════════════════════════════════════════════════════════════════

    async def _print_balance_report(self):
        """Her 10 pencerede bakiye raporu yazdır."""
        try:
            balance = await self.claimer._get_balance_with_retry(self.executor)
        except Exception:
            return  # API geçici hata — raporu atla, botu durdurma
        change = balance - self._initial_balance
        sign = "+" if change >= 0 else ""

        print(
            f"\n  {'=' * 46}\n"
            f"  BAKIYE RAPORU\n"
            f"  Bakiye:     ${balance}\n"
            f"  Baslangic:  ${self._initial_balance}\n"
            f"  Degisim:    {sign}${change}\n"
            f"  {'=' * 46}\n"
        )

        if balance < cfg.EMERGENCY_BALANCE_MIN:
            self._stop_reason = (
                f"Bakiye ${balance} < ${cfg.EMERGENCY_BALANCE_MIN} minimum"
            )
            self._print_emergency_stop()
            self._running = False

    # ══════════════════════════════════════════════════════════════════
    #  İŞLEM DENEME
    # ══════════════════════════════════════════════════════════════════

    async def _try_trade(self, window_ts: int, remaining: int):
        """
        Tüm TRADING_ASSETS üzerinde sinyal üret, ilk uygun asset'te emir gönder.
        Pencere başına en fazla bir başarılı işlem (_traded_this_window).
        Aynı asset için pencere başına en fazla bir emir denemesi
        (_order_attempted_this_window); BTC emri dolmazsa ETH aynı pencerede denenebilir.
        """
        async with self._try_trade_lock:
            if self._traded_this_window == window_ts:
                return

            for asset in cfg.TRADING_ASSETS:
                if self._traded_this_window == window_ts:
                    return
                traded = await self._try_trade_for_asset(
                    asset, window_ts, remaining
                )
                if traded:
                    return

    async def _try_trade_for_asset(
        self, asset: str, window_ts: int, remaining: int
    ) -> bool:
        """Tek bir asset için sinyal üret ve uygunsa emir gönder. True ise trade yapıldı."""
        if asset in self._order_attempted_this_window:
            return False

        exchange_data = self.exchange.get_price(asset)
        oracle_data = self.oracle.get_oracle_data(asset)
        window_open_price = self.oracle.get_window_open_price(window_ts, asset)

        market = await self.resolver.get_active_market(asset)
        if not market:
            log_skip(f"[{asset}] Aktif piyasa bulunamadi", window_ts)
            return False

        if not self.resolver.is_tradeable(market):
            log_skip(f"[{asset}] Piyasa islem yapilabilir degil (fiyat/likidite)", window_ts)
            return False

        up_ask = market.get("up_best_ask")
        down_ask = market.get("down_best_ask")
        sig = self.signal.generate_signal(
            exchange_data,
            oracle_data,
            window_open_price,
            up_best_ask=up_ask,
            down_best_ask=down_ask,
        )

        if sig["direction"] == "SKIP":
            log_skip(f"[{asset}] {sig['reason']}", window_ts)
            return False

        if sig["direction"] == "UP":
            token_id = market["token_id_up"]
            token_price = market["up_best_ask"]
            book_size = market.get("up_liquidity", Decimal("999"))
        else:
            token_id = market["token_id_down"]
            token_price = market["down_best_ask"]
            book_size = market.get("down_liquidity", Decimal("999"))

        if token_price is not None and token_price > cfg.MAX_TOKEN_PRICE:
            log_skip(
                f"[{asset}] Token fiyati ${token_price} > max ${cfg.MAX_TOKEN_PRICE} "
                f"(run.py guvenlik)",
                window_ts,
            )
            return False

        # Confidence-price gate: Kelly modunda gereksiz (Kelly kendi filtreler),
        # ama güvenlik için temel kontrol
        if not cfg.USE_KELLY:
            required_confidence = int(token_price * 100)
            if sig["confidence"] < required_confidence:
                log_skip(
                    f"[{asset}] Confidence-price gate: conf {sig['confidence']} < "
                    f"gerekli {required_confidence} (token ${token_price})",
                    window_ts,
                )
                return False

        balance = await self.executor.get_balance()
        can_trade, reason = self.risk.can_trade(
            balance_usdc=balance,
            token_price=token_price,
            book_size=book_size,
        )

        if not can_trade:
            log_skip(f"[{asset}] Risk: {reason} (bakiye: ${balance})", window_ts)
            if "Cooldown" in reason:
                stats = self.risk.get_stats()
                log_cooldown(stats["consecutive_losses"], 0)
            return False

        # ── Pozisyon boyutu: Kelly veya sabit ────────────────────────
        kelly_info = None
        if cfg.USE_KELLY:
            position_size, kelly_info = self.risk.calculate_kelly_size(
                balance_usdc=balance,
                token_price=token_price,
                confidence=sig["confidence"],
                book_size=book_size,
            )
            if position_size <= 0:
                log_skip(
                    f"[{asset}] Kelly SKIP: {kelly_info['reason']}",
                    window_ts,
                )
                return False
        else:
            position_size = self.risk.calculate_position_size(
                confidence=sig["confidence"],
                token_price=token_price,
                book_size=book_size,
            )

        if not cfg.DRY_RUN:
            shares_est = (position_size / token_price).quantize(
                Decimal("0.01")
            ) if token_price > 0 else Decimal("0")
            t = _now_str()
            print(f"\n  [{t}] {'=' * 42}")
            print(f"  [{t}] [{asset}] GERCEK ISLEM #{self._production_trade_count + 1}")
            print(f"  [{t}] [{asset}] Yon: {sig['direction']} | Confidence: {sig['confidence']}")
            print(f"  [{t}] [{asset}] Delta: {'+' if sig['delta_percent'] > 0 else ''}{sig['delta_percent']:.4f}% | Oracle Lag: {sig['oracle_lag']}s")
            if kelly_info:
                print(
                    f"  [{t}] [{asset}] Kelly: p={kelly_info['p']}, "
                    f"f*={kelly_info['f_star']:.3f}, "
                    f"f_adj={kelly_info['f_adj']:.3f}"
                )
            print(f"  [{t}] [{asset}] Token fiyat: ${token_price} | Shares: ~{shares_est} | Maliyet: ${position_size}")
            print(f"  [{t}] [{asset}] Bakiye ONCE: ${balance}")
            print(f"  [{t}] {'=' * 42}")

        self._order_attempted_this_window.add(asset)

        order_result = await self.executor.place_order(
            token_id=token_id,
            amount_usdc=position_size,
            fair_price=token_price,
        )

        if order_result["success"]:
            # Emir API'ye ulaştı — hemen kilitle (fill bekleme öncesi double-order önler)
            self._committed_trade_window = window_ts
            self._traded_this_window = window_ts
            self._consecutive_order_rejects = 0
            order_type = order_result.get("order_type_used", "GTC")

            if not cfg.DRY_RUN:
                filled, spent, balance_after = await self._check_fill(
                    balance, order_result, order_type, attempt=1
                )

                if not filled:
                    retry_size = max(
                        position_size,
                        Decimal("5") * cfg.MAX_TOKEN_PRICE,
                    )
                    if retry_size > balance_after:
                        retry_size = balance_after
                    t = _now_str()
                    print(
                        f"  [{t}] [{asset}] Retry: ${retry_size} ile MAX fiyat "
                        f"(${cfg.MAX_TOKEN_PRICE}) deneniyor..."
                    )
                    retry_result = await self.executor.place_order(
                        token_id=token_id,
                        amount_usdc=retry_size,
                        fair_price=cfg.MAX_TOKEN_PRICE,
                    )
                    if retry_result["success"]:
                        order_result = retry_result
                        order_type = "GTC-RETRY"
                        filled, spent, balance_after = await self._check_fill(
                            balance, order_result, order_type, attempt=2
                        )
                    else:
                        t = _now_str()
                        print(
                            f"  [{t}] [{asset}] Retry emir hatasi: "
                            f"{retry_result.get('error', '?')}"
                        )

                    if not filled:
                        t = _now_str()
                        print(
                            f"  [{t}] [{asset}] Retry de DOLMADI. Bu pencerede "
                            f"emir YOK.\n"
                        )
                        return False

                order_result["total_cost"] = spent
                actual_shares = order_result["shares"]
                if actual_shares > 0:
                    actual_price = (spent / actual_shares).quantize(
                        Decimal("0.001")
                    )
                else:
                    actual_price = order_result["price_paid"]
                order_result["price_paid"] = actual_price

                t = _now_str()
                print(
                    f"  [{t}] [{asset}] DOLDU [{order_type}] — Fill: ${actual_price}/share, "
                    f"maliyet: ${spent:.2f}, shares: {actual_shares}"
                )

            self._production_trade_count += 1

            self.claimer.register_trade(window_ts, {
                "token_id": token_id,
                "direction": sig["direction"],
                "cost_usdc": order_result["total_cost"],
                "shares": order_result["shares"],
                "token_price": order_result["price_paid"],
            })

            trade_log = {
                "window_ts": window_ts,
                "asset": asset,
                "direction": sig["direction"],
                "exchange_price": str(exchange_data.get("average", "0")),
                "oracle_price": str(oracle_data.get("price", "0")),
                "window_open_price": str(window_open_price or "0"),
                "delta_percent": str(sig["delta_percent"]),
                "oracle_lag_seconds": sig["oracle_lag"],
                "confidence": sig["confidence"],
                "token_price_paid": str(order_result["price_paid"]),
                "shares": str(order_result["shares"]),
                "total_cost_usdc": str(order_result["total_cost"]),
                "order_id": order_result["order_id"],
                "dry_run": order_result["dry_run"],
                "production_trade_num": self._production_trade_count,
                "fill_verified": True,
            }
            if kelly_info:
                trade_log["kelly_p"] = str(kelly_info["p"])
                trade_log["kelly_f_star"] = str(kelly_info["f_star"])
                trade_log["kelly_f_adj"] = str(kelly_info["f_adj"])
            log_trade(trade_log)
            return True
        else:
            error_type = order_result.get("error_type", "post_send")
            error_msg = order_result.get("error", "")

            # ── Geoblock: VPS bölgesi Polymarket tarafından engellendi ──
            if error_type == "geoblock":
                t = _now_str()
                print(f"""
  [{t}] \033[91m\033[1m{'=' * 44}\033[0m
  [{t}] \033[91m\033[1m  POLYMARKET GEOBLOCK (403)\033[0m
  [{t}] \033[91m\033[1m{'=' * 44}\033[0m
  [{t}]  Sunucunun bulundugu bolge Polymarket tarafindan
  [{t}]  engellenmis. Emirler gonderilemez.
  [{t}]
  [{t}]  Cozum secenekleri:
  [{t}]   1. VPN / SOCKS5 proxy kur (onerilir):
  [{t}]      apt install dante-server  veya  3proxy
  [{t}]      .env'e HTTPS_PROXY=socks5://127.0.0.1:1080 ekle
  [{t}]   2. Sunucuyu baska bir bolgeye (US/UK) tasi
  [{t}]   3. Yerel makinenden calistir
  [{t}]
  [{t}]  Bot durduruluyor (geoblock — emir sayaci artmaz)
  [{t}] \033[91m\033[1m{'=' * 44}\033[0m
""")
                log_error("GEOBLOCK: Bolge engeli — bot durduruluyor", {"error": error_msg})
                self._stop_reason = "Geoblock (403) — VPN/proxy gerekli"
                self._running = False
                return True

            if error_type != "pre_send":
                self._consecutive_order_rejects += 1

            if "fee" in error_msg.lower():
                print(
                    f"\n  {_C.RED}{_C.BOLD}"
                    f"  FEE HATASI: {error_msg}\n"
                    f"  order_executor.py'daki fee_rate_bps degerini guncelle."
                    f"{_C.RESET}\n"
                )
                log_error("Fee hatasi — bot durduruluyor", {"error": error_msg})
                self._stop_reason = f"Fee hatasi: {error_msg}"
                self._running = False
                return True

            log_error(
                f"[{asset}] Emir basarisiz ({error_type}): {error_msg}",
                {"token_id": token_id, "window_ts": window_ts, "asset": asset},
            )

            if self._consecutive_order_rejects >= cfg.EMERGENCY_ORDER_REJECTS:
                self._stop_reason = (
                    f"{self._consecutive_order_rejects} ardisik emir reddi"
                )
                self._print_emergency_stop()
                self._running = False
                return True

            return False

    # ── Fill kontrolü (retry'da yeniden kullanılır) ─────────────────

    async def _check_fill(
        self, balance_before: Decimal, order_result: dict,
        order_type: str, attempt: int = 1,
    ) -> tuple:
        """Emrin dolup dolmadığını kontrol et. (filled, spent, balance_after)"""
        wait = 20 if attempt == 1 else 8
        await asyncio.sleep(wait)
        await self.executor.cancel_all_orders()
        await asyncio.sleep(2)
        balance_after = await self.executor.get_balance()
        spent = balance_before - balance_after
        expected_cost = order_result["total_cost"]

        if spent < expected_cost * Decimal("0.3"):
            t = _now_str()
            print(
                f"\n  [{t}] DOLMADI [{order_type}] — "
                f"Bakiye: ${balance_before} → ${balance_after}"
            )
            return False, spent, balance_after
        return True, spent, balance_after

    # ══════════════════════════════════════════════════════════════════
    #  PENCERE KAPANIŞI
    # ══════════════════════════════════════════════════════════════════

    async def _handle_window_close(self, window_ts: int):
        """Pencere kapandığında sonuç kontrol et ve PnL kaydet."""

        if window_ts in self._settled_window_closes:
            return

        # ── Ertelenmiş uzlaştırma: Geç gelen redeem'leri kontrol et ──
        if not cfg.DRY_RUN and self.claimer.pending_count > 0:
            reconciled = await self.claimer.reconcile_pending(
                self.executor, self._initial_balance
            )
            for claim in reconciled:
                await self._process_claim_result(claim)

        claims = await self.claimer.check_and_claim(
            self.executor, window_ts, self.exchange, self.oracle
        )
        for claim in claims:
            await self._process_claim_result(claim)

        self._settled_window_closes.add(window_ts)
        if len(self._settled_window_closes) > 200:
            keep = sorted(self._settled_window_closes)[-120:]
            self._settled_window_closes = set(keep)

    async def _process_claim_result(self, claim: dict):
        """Tek bir claim sonucunu işle (WIN/LOSS/PENDING/UNKNOWN)."""
        cost_usdc = claim.get("cost_usdc", Decimal("0"))
        result = claim.get("result", "UNKNOWN")
        window_ts = claim.get("window_ts", 0)
        is_reconciled = claim.get("reconciled", False)

        if result == "WIN":
            payout = claim.get("payout", Decimal("0"))
            if payout <= 0:
                payout = claim.get("shares", Decimal("5"))
            profit = payout - cost_usdc
            self.risk.record_trade("WIN", profit)
            self.risk.save_state(self._risk_state_path)
        elif result == "LOSS" and not is_reconciled:
            self.risk.record_trade("LOSS", -cost_usdc)
            self.risk.save_state(self._risk_state_path)
            profit = -cost_usdc
            payout = Decimal("0")
        elif result == "LOSS" and is_reconciled:
            profit = -cost_usdc
            payout = Decimal("0")
        elif result == "PENDING":
            t = _now_str()
            print(
                f"  [{t}] \033[93m\033[1mPENDING\033[0m — "
                f"Redeem henuz gelmedi, ertelenmis uzlastirma beklenecek"
            )
            log_result({
                "window_ts": window_ts,
                "result": "PENDING",
                "payout_usdc": "0",
                "cost_usdc": str(cost_usdc),
                "profit_usdc": "0",
                "daily_pnl": str(self.risk.get_stats()["daily_pnl"]),
                "wins": self.risk.get_stats()["wins"],
                "losses": self.risk.get_stats()["losses"],
                "win_rate": str(self.risk.get_stats()["win_rate"]),
                "consecutive_losses": self.risk.get_stats()["consecutive_losses"],
                "production_trade_num": self._production_trade_count,
                "dry_run": cfg.DRY_RUN,
            })
            return
        elif result == "UNKNOWN" and cost_usdc > 0:
            log_error(
                f"Sonuc belirlenemedi, LOSS olarak kaydediliyor",
                {"window_ts": window_ts, "error": claim.get("error")},
            )
            self.risk.record_trade("LOSS", -cost_usdc)
            self.risk.save_state(self._risk_state_path)
            profit = -cost_usdc
            payout = Decimal("0")
            result = "LOSS"
        else:
            return

        stats = self.risk.get_stats()

        if not cfg.DRY_RUN:
            try:
                balance_after = await self.executor.get_balance()
            except Exception:
                balance_after = "?"
            t = _now_str()
            res_color = _C.GREEN if result == "WIN" else _C.RED
            tag = " (RECONCILED)" if is_reconciled else ""
            print(f"\n  [{t}] {'=' * 42}")
            print(f"  [{t}] {res_color}{_C.BOLD}SONUC: {result}{tag}{_C.RESET}")
            if result == "WIN":
                print(f"  [{t}] Kazanc: ${payout} | Kar: +${profit}")
            else:
                print(f"  [{t}] Kayip: -${abs(cost_usdc)}")
            print(f"  [{t}] Bakiye SONRA: ${balance_after}")
            print(
                f"  [{t}] Gunluk P&L: ${stats['daily_pnl']} | "
                f"{stats['wins']}W/{stats['losses']}L "
                f"(%{stats['win_rate']})"
            )
            print(f"  [{t}] {'=' * 42}")

        log_result({
            "window_ts": window_ts,
            "result": result,
            "payout_usdc": str(payout),
            "cost_usdc": str(cost_usdc),
            "profit_usdc": str(profit),
            "daily_pnl": str(stats["daily_pnl"]),
            "wins": stats["wins"],
            "losses": stats["losses"],
            "win_rate": str(stats["win_rate"]),
            "consecutive_losses": stats["consecutive_losses"],
            "production_trade_num": self._production_trade_count,
            "dry_run": cfg.DRY_RUN,
            "reconciled": is_reconciled,
        })

        if not cfg.DRY_RUN:
            if await self._check_emergency_stop():
                self._print_emergency_stop()
                self._running = False

    # ══════════════════════════════════════════════════════════════════
    #  TEMİZ KAPANIŞ
    # ══════════════════════════════════════════════════════════════════

    async def shutdown(self):
        """Tüm bağlantıları kapat ve özet yazdır."""
        print("\n\n  Bot durduruluyor...")

        if not cfg.DRY_RUN:
            await self.executor.cancel_all_orders()

        await self.exchange.stop()
        await self.oracle.stop()
        await self.resolver.close()

        stats = self.risk.get_stats()
        print_summary(stats)

        if cfg.DRY_RUN:
            verdict = self.risk.get_dry_run_verdict()
            print_dry_run_report(stats, verdict)

        if not cfg.DRY_RUN:
            try:
                final_balance = await self.executor.get_balance()
            except Exception:
                final_balance = "?"
            change = "?"
            if isinstance(final_balance, Decimal):
                change = final_balance - self._initial_balance
            print(
                f"  Baslangic bakiye: ${self._initial_balance}\n"
                f"  Final bakiye:     ${final_balance}\n"
                f"  Net degisim:      ${change}\n"
            )

        print("  Bot durduruldu.")


# ══════════════════════════════════════════════════════════════════════
#  CLI GİRİŞ NOKTASI
# ══════════════════════════════════════════════════════════════════════

_LOCK_FILE = Path(__file__).resolve().parent / "data" / "bot.pid"


def _acquire_pid_lock():
    """Tek instance garantisi: PID lock dosyası ile çift çalışmayı engelle."""
    if _LOCK_FILE.exists():
        try:
            old_pid = int(_LOCK_FILE.read_text().strip())
            try:
                os.kill(old_pid, 0)
                print(
                    f"\n  HATA: Bot zaten calisiyor (PID {old_pid}).\n"
                    f"  Durdurmak icin: kill {old_pid}\n"
                    f"  Zorla baslatmak icin: rm {_LOCK_FILE} && python run.py\n"
                )
                sys.exit(1)
            except OSError:
                pass
        except (ValueError, FileNotFoundError):
            pass

    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE.write_text(str(os.getpid()))
    atexit.register(_release_pid_lock)


def _release_pid_lock():
    """Çıkışta lock dosyasını temizle."""
    try:
        if _LOCK_FILE.exists():
            stored_pid = int(_LOCK_FILE.read_text().strip())
            if stored_pid == os.getpid():
                _LOCK_FILE.unlink()
    except (ValueError, OSError):
        pass


def main():
    """Komut satırı argümanlarını parse et ve botu başlat."""
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, OSError, ValueError):
        pass

    if "--dry-run" in sys.argv:
        cfg.DRY_RUN = True
        print("  [--dry-run] Kuru calisma modu aktif, emir gonderilmeyecek.\n")

    if "--claim-only" in sys.argv:
        print("  [--claim-only] Sadece claim modu.\n")
        asyncio.run(_claim_only())
        return

    _acquire_pid_lock()

    bot = Bot()

    loop = asyncio.new_event_loop()

    def handle_signal():
        bot._running = False

    try:
        loop.add_signal_handler(signal.SIGINT, handle_signal)
        loop.add_signal_handler(signal.SIGTERM, handle_signal)
    except NotImplementedError:
        pass

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        bot._running = False
        loop.run_until_complete(bot.shutdown())
    finally:
        loop.close()


async def _claim_only():
    """Sadece açık pozisyonları kontrol et ve listele."""
    cfg.print_summary()
    executor = OrderExecutor(cfg)
    await executor.initialize()

    print("  Pozisyonlar kontrol ediliyor...")
    positions = await executor.get_positions()

    if positions:
        for pos in positions:
            if isinstance(pos, dict):
                tid = pos.get("asset", pos.get("token_id", "?"))
                size = pos.get("size", "0")
            else:
                tid = getattr(pos, "asset", getattr(pos, "token_id", "?"))
                size = getattr(pos, "size", "0")
            print(f"  Token: {str(tid)[:12]}... | Size: {size}")
    else:
        print("  Acik pozisyon bulunamadi.")

    print("  Tamamlandi.")


if __name__ == "__main__":
    main()
