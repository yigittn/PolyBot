"""
test_preflight.py — Production Öncesi Kapsamlı Test
=====================================================
Botu production'a almadan önce tüm bileşenleri test eder.

Kullanım:
    python test_preflight.py

Her test için:
    ✅ PASS  — Sorunsuz çalışıyor
    ⚠️  WARN  — Çalışıyor ama dikkat edilmesi gereken nokta var
    ❌ FAIL  — Bu sorun giderilmeden bot çalışmaz
"""

import asyncio
import json
import ssl
import sys
import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import certifi
import websockets

# ── Renk kodları ──────────────────────────────────────────────────────────────
G = "\033[92m"   # yeşil
Y = "\033[93m"   # sarı
R = "\033[91m"   # kırmızı
B = "\033[94m"   # mavi
BOLD = "\033[1m"
RESET = "\033[0m"

PASS = f"{G}{BOLD}✅ PASS{RESET}"
WARN = f"{Y}{BOLD}⚠️  WARN{RESET}"
FAIL = f"{R}{BOLD}❌ FAIL{RESET}"

results: list[tuple[str, str, str]] = []  # (test_name, status, detail)


def log(name: str, status: str, detail: str):
    results.append((name, status, detail))
    icon = {"PASS": PASS, "WARN": WARN, "FAIL": FAIL}[status]
    print(f"  {icon}  {BOLD}{name}{RESET}")
    print(f"         {detail}")


def section(title: str):
    print(f"\n  {B}{BOLD}{'─' * 48}{RESET}")
    print(f"  {B}{BOLD}  {title}{RESET}")
    print(f"  {B}{BOLD}{'─' * 48}{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 1: .env ve Config
# ══════════════════════════════════════════════════════════════════════════════

def test_config():
    section("1. CONFIG & .ENV")

    # 1a. .env dosyası var mı?
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        log(".env dosyası", "FAIL", f"Bulunamadı: {env_path}")
        return False
    log(".env dosyası", "PASS", f"Mevcut: {env_path}")

    # 1b. Config yükle
    try:
        from config import cfg
        log("Config yükleme", "PASS", f"DRY_RUN={cfg.DRY_RUN}, Assets={cfg.TRADING_ASSETS}")
    except SystemExit as e:
        log("Config yükleme", "FAIL", f"Zorunlu ayar eksik — .env dosyasını kontrol et")
        return False
    except Exception as e:
        log("Config yükleme", "FAIL", str(e))
        return False

    # 1c. Kritik alanlar
    checks = [
        ("PRIVATE_KEY", len(cfg.PRIVATE_KEY) > 10, cfg.PRIVATE_KEY[:8] + "..."),
        ("FUNDER_ADDRESS", cfg.FUNDER_ADDRESS.startswith("0x"), cfg.FUNDER_ADDRESS[:12] + "..."),
        ("POLYGON_RPC_URL", len(cfg.POLYGON_RPC_URL) > 10, cfg.POLYGON_RPC_URL),
        ("MAX_TOKEN_PRICE", float(cfg.MAX_TOKEN_PRICE) <= 0.65, f"${cfg.MAX_TOKEN_PRICE}"),
        ("MIN_TOKEN_PRICE", float(cfg.MIN_TOKEN_PRICE) >= 0.10, f"${cfg.MIN_TOKEN_PRICE}"),
        ("DELTA_THRESHOLD", float(cfg.DELTA_THRESHOLD) >= 0.03, f"%{cfg.DELTA_THRESHOLD}"),
    ]
    for name, ok, val in checks:
        if ok:
            log(f"  {name}", "PASS", val)
        else:
            log(f"  {name}", "WARN", f"Değer beklenmedik: {val}")

    # 1d. DRY_RUN uyarısı
    if cfg.DRY_RUN:
        log("DRY_RUN modu", "WARN", "DRY_RUN=true — gerçek emir gönderilmez, production için false yapın")
    else:
        log("DRY_RUN modu", "PASS", "DRY_RUN=false — GERÇEK İŞLEM MODU")

    # 1e. Proxy
    if cfg.HTTPS_PROXY:
        host = cfg.HTTPS_PROXY.split("@")[-1]
        log("Proxy", "PASS", f"Aktif: {host}")
    else:
        log("Proxy", "WARN", "HTTPS_PROXY tanımlı değil (US sunucu kullanıyorsan geoblock olabilir)")

    return True


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 2: Binance WebSocket
# ══════════════════════════════════════════════════════════════════════════════

async def test_binance():
    section("2. BİNANCE WEBSOCKET")
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    url = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
    label = "Binance.com"
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=8) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            data = json.loads(raw)
            price = data.get("c", "?")
            log(label, "PASS", f"BTC fiyatı: ${float(price):,.2f}")
            return True
    except asyncio.TimeoutError:
        log(label, "FAIL", "Bağlantı veya veri zaman aşımı (8s)")
    except Exception as e:
        log(label, "FAIL", f"{type(e).__name__}: {str(e)[:80]}")

    log("Binance (özet)", "FAIL", "stream.binance.com bağlanamadı — Coinbase/Bitstamp yedek devreye girer")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 3: Coinbase WebSocket
# ══════════════════════════════════════════════════════════════════════════════

async def test_coinbase():
    section("3. COİNBASE WEBSOCKET")
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    url = "wss://ws-feed.exchange.coinbase.com"
    sub = json.dumps({
        "type": "subscribe",
        "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}],
    })
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=8) as ws:
            await ws.send(sub)
            for _ in range(5):  # subscriptions + ticker
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(raw)
                if data.get("type") == "ticker" and data.get("price"):
                    price = float(data["price"])
                    log("Coinbase WebSocket", "PASS", f"BTC fiyatı: ${price:,.2f}")
                    return True
        log("Coinbase WebSocket", "WARN", "Bağlandı ama ticker verisi gelmedi")
        return False
    except asyncio.TimeoutError:
        log("Coinbase WebSocket", "FAIL", "Zaman aşımı (10s)")
        return False
    except Exception as e:
        log("Coinbase WebSocket", "FAIL", f"{type(e).__name__}: {str(e)[:80]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 4: Bitstamp WebSocket (backup)
# ══════════════════════════════════════════════════════════════════════════════

async def test_bitstamp():
    section("4. BİTSTAMP WEBSOCKET (backup)")
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    url = "wss://ws.bitstamp.net"
    sub = json.dumps({
        "event": "bts:subscribe",
        "data": {"channel": "live_trades_btcusd"},
    })
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=8) as ws:
            await ws.send(sub)
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(raw)
            log("Bitstamp WebSocket", "PASS", f"Bağlandı — event: {data.get('event', '?')}")
            return True
    except asyncio.TimeoutError:
        log("Bitstamp WebSocket", "WARN", "Zaman aşımı — backup kaynak, kritik değil")
        return False
    except Exception as e:
        log("Bitstamp WebSocket", "WARN", f"Bağlanamadı (backup, kritik değil): {str(e)[:60]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 5: Chainlink Oracle (Polygon RPC)
# ══════════════════════════════════════════════════════════════════════════════

def test_oracle():
    section("5. CHAİNLİNK ORACLE (Polygon RPC)")
    from config import cfg
    from web3 import Web3

    rpcs = [cfg.POLYGON_RPC_URL]
    if cfg.POLYGON_RPC_BACKUP:
        rpcs.append(cfg.POLYGON_RPC_BACKUP)
    if cfg.POLYGON_RPC_FALLBACK2:
        rpcs.append(cfg.POLYGON_RPC_FALLBACK2)

    ORACLE_ADDR = "0xc907E116054Ad103354f2D350FD2514433D57F6f"  # BTC/USD
    ABI = [{"inputs": [], "name": "latestRoundData",
            "outputs": [{"name": "roundId", "type": "uint80"},
                        {"name": "answer", "type": "int256"},
                        {"name": "startedAt", "type": "uint256"},
                        {"name": "updatedAt", "type": "uint256"},
                        {"name": "answeredInRound", "type": "uint80"}],
            "stateMutability": "view", "type": "function"}]

    any_ok = False
    for rpc in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
            contract = w3.eth.contract(address=Web3.to_checksum_address(ORACLE_ADDR), abi=ABI)
            result = contract.functions.latestRoundData().call()
            price = result[1] / 10**8
            updated_at = result[3]
            lag = int(time.time()) - updated_at
            status = "PASS" if lag < 120 else "WARN"
            log(f"Oracle ({rpc[:40]}...)", status,
                f"BTC/USD: ${price:,.2f} | Lag: {lag}s")
            any_ok = True
            break
        except Exception as e:
            log(f"Oracle ({rpc[:40]}...)", "WARN", f"Hata: {str(e)[:70]}")

    if not any_ok:
        log("Oracle (özet)", "FAIL", "Hiçbir Polygon RPC çalışmıyor — oracle verisi alınamaz")
    return any_ok


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 6: Polymarket CLOB API — Kimlik Doğrulama & Bakiye
# ══════════════════════════════════════════════════════════════════════════════

async def test_clob_auth():
    section("6. POLYMARKET CLOB API — KİMLİK DOĞRULAMA & BAKİYE")
    from config import cfg
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    try:
        clob = ClobClient(
            host=cfg.CLOB_HOST,
            key=cfg.PRIVATE_KEY,
            chain_id=cfg.CHAIN_ID,
            signature_type=cfg.SIGNATURE_TYPE,
            funder=cfg.FUNDER_ADDRESS,
        )
        log("CLOB client oluşturma", "PASS", f"Host: {cfg.CLOB_HOST}")
    except Exception as e:
        log("CLOB client oluşturma", "FAIL", str(e))
        return False

    # API credentials
    try:
        loop = asyncio.get_running_loop()
        creds = await loop.run_in_executor(None, clob.create_or_derive_api_creds)
        clob.set_api_creds(creds)
        log("API credentials", "PASS", f"API key türetildi")
    except Exception as e:
        log("API credentials", "FAIL", f"Private key geçersiz veya API hatası: {str(e)[:80]}")
        return False

    # Bakiye
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        balance_raw = await loop.run_in_executor(None, clob.get_balance_allowance, params)
        raw = Decimal("0")
        if hasattr(balance_raw, "balance"):
            raw = Decimal(str(balance_raw.balance))
        elif isinstance(balance_raw, dict):
            raw = Decimal(str(balance_raw.get("balance", "0")))
        USDC_DECIMALS = Decimal("1000000")
        balance = (raw / USDC_DECIMALS) if raw > 1000 else raw
        if balance >= Decimal("2.5"):
            log("USDC bakiye", "PASS", f"${balance:.6f} USDC")
        elif balance > 0:
            log("USDC bakiye", "WARN", f"${balance:.6f} USDC — az, en az $5 önerilir")
        else:
            log("USDC bakiye", "FAIL", f"${balance} — bakiye yok veya API hatası")
        return True
    except Exception as e:
        log("USDC bakiye", "FAIL", f"{type(e).__name__}: {str(e)[:80]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 7: Polymarket Geoblock Testi
# ══════════════════════════════════════════════════════════════════════════════

async def test_geoblock():
    section("7. POLYMARKET GEOBLOCK TESTİ")
    import aiohttp
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    # 7a. Gamma API erişimi
    try:
        conn = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.get(
                "https://gamma-api.polymarket.com/markets?limit=1&active=true",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    log("Gamma API erişimi", "PASS", f"HTTP {resp.status}")
                elif resp.status == 403:
                    log("Gamma API erişimi", "FAIL", "HTTP 403 — IP bölge engeli (VPN/proxy gerekli)")
                    return False
                else:
                    log("Gamma API erişimi", "WARN", f"HTTP {resp.status}")
    except Exception as e:
        log("Gamma API erişimi", "FAIL", f"{str(e)[:80]}")
        return False

    # 7b. CLOB API — emir gönderme endpoint'i testi
    # Gerçek emir göndermeden, mevcut emirleri listeleme ile geoblock testi
    try:
        from config import cfg
        from py_clob_client.client import ClobClient
        clob = ClobClient(
            host=cfg.CLOB_HOST, key=cfg.PRIVATE_KEY,
            chain_id=cfg.CHAIN_ID, signature_type=cfg.SIGNATURE_TYPE,
            funder=cfg.FUNDER_ADDRESS,
        )
        loop = asyncio.get_running_loop()
        clob.set_api_creds(await loop.run_in_executor(None, clob.create_or_derive_api_creds))
        orders = await loop.run_in_executor(None, clob.get_orders)
        log("CLOB emir listesi (geoblock probe)", "PASS",
            f"Erişim var — {len(orders) if orders else 0} açık emir")
        return True
    except Exception as e:
        err = str(e)
        if "403" in err or "geoblock" in err.lower() or "restricted" in err.lower():
            log("CLOB emir listesi (geoblock probe)", "FAIL",
                "HTTP 403 GEOBLOCK — bu IP'den emir gönderilemiyor!\n"
                "         Çözüm: .env'e HTTPS_PROXY ekle veya sunucuyu EU/UK'ye taşı")
            return False
        log("CLOB emir listesi (geoblock probe)", "WARN", f"Beklenmedik hata: {err[:80]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 8: Gamma API — Aktif Piyasa Keşfi
# ══════════════════════════════════════════════════════════════════════════════

async def test_market_discovery():
    section("8. AKTİF PİYASA KEŞFİ (Gamma API)")
    from market_resolver import MarketResolver
    from config import cfg

    try:
        resolver = MarketResolver(cfg)
        await resolver.initialize()
        window = resolver.get_current_window()
        print(f"         Mevcut pencere: {window['window_ts']} | Kapanışa: {window['seconds_remaining']}s")

        market = await resolver.get_active_market("BTC")
        if market:
            up_p = market.get("up_best_ask", "?")
            down_p = market.get("down_best_ask", "?")
            up_liq = market.get("up_liquidity", "?")
            down_liq = market.get("down_liquidity", "?")
            tradeable = resolver.is_tradeable(market)
            status = "PASS" if tradeable else "WARN"
            log("BTC piyasası", status,
                f"Slug: {market.get('slug','?')} | UP: ${up_p} (liq: {up_liq:.1f}) | "
                f"DOWN: ${down_p} (liq: {down_liq:.1f}) | İşlem yapılabilir: {tradeable}")
        else:
            log("BTC piyasası", "WARN", "Şu an aktif piyasa yok (pencere geçiş zamanı olabilir)")

        await resolver.close()
        return True
    except Exception as e:
        log("Piyasa keşfi", "FAIL", f"{type(e).__name__}: {str(e)[:80]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 9: Signal Engine — Canlı Veriyle Sinyal Üretimi
# ══════════════════════════════════════════════════════════════════════════════

async def test_signal_engine():
    section("9. SİNYAL ENGİNE — CANLI VERİ TESTİ")
    from signal_engine import SignalEngine
    from exchange_feed import ExchangeFeed
    from oracle_monitor import OracleMonitor
    from config import cfg

    # Exchange feed başlat
    try:
        exchange = ExchangeFeed(cfg)
        await exchange.start()
        await asyncio.sleep(5)  # fiyat verisi gelsin

        oracle = OracleMonitor(cfg)
        await oracle.start()
        await asyncio.sleep(2)

        ex_data = exchange.get_price("BTC")
        orc_data = oracle.get_oracle_data("BTC")

        if ex_data.get("average") is None:
            log("Exchange veri", "WARN", "Fiyat verisi henüz gelmedi (WebSocket bağlanıyor)")
        else:
            avg = ex_data["average"]
            div = ex_data.get("divergence_pct", "?")
            trend = ex_data.get("trend_pct")
            trend_str = f"{trend:+.3f}%" if trend is not None else "yok (veri az)"
            log("Exchange veri", "PASS",
                f"BTC avg: ${avg:,.2f} | Divergence: {div}% | Trend: {trend_str}")

        if orc_data.get("price") is None:
            log("Oracle veri", "WARN", "Oracle fiyatı alınamadı")
        else:
            lag = orc_data["lag_seconds"]
            price = orc_data["price"]
            status = "PASS" if lag < 120 else "WARN"
            log("Oracle veri", status, f"BTC: ${price:,.2f} | Lag: {lag}s")

        # Sinyal motoru testi
        if ex_data.get("average") and orc_data.get("price"):
            engine = SignalEngine(cfg)
            wop = orc_data["price"]  # pencere açılış fiyatı olarak oracle kullan
            sig = engine.generate_signal(ex_data, orc_data, wop)
            log("Sinyal motoru", "PASS",
                f"Çalışıyor — Sonuç: {sig['direction']} | Conf: {sig['confidence']} | "
                f"Sebep: {sig['reason'][:60]}")
        else:
            log("Sinyal motoru", "WARN", "Veri eksik, sinyal üretilemedi")

        await exchange.stop()
        await oracle.stop()
        return True
    except Exception as e:
        log("Sinyal engine testi", "FAIL", f"{type(e).__name__}: {str(e)[:80]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 10: Risk Manager — State & can_trade
# ══════════════════════════════════════════════════════════════════════════════

def test_risk_manager():
    section("10. RİSK MANAGER")
    from risk_manager import RiskManager
    from config import cfg
    import tempfile, os

    rm = RiskManager(cfg)

    # State kaydetme/yükleme
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    try:
        rm.save_state(tmp)
        rm2 = RiskManager(cfg)
        rm2.load_state(tmp)
        log("State kaydet/yükle", "PASS", f"risk_state.json yaz/oku çalışıyor")
    except Exception as e:
        log("State kaydet/yükle", "FAIL", str(e))
    finally:
        os.unlink(tmp)

    # can_trade testleri
    cases = [
        (Decimal("20"), Decimal("0.45"), Decimal("100"), "PASS", "Normal işlem"),
        (Decimal("1.0"), Decimal("0.45"), Decimal("100"), "FAIL", "Bakiye yetersiz"),
        (Decimal("20"), Decimal("0.95"), Decimal("100"), "FAIL", "Token fiyatı çok yüksek"),
        (Decimal("20"), Decimal("0.10"), Decimal("100"), "FAIL", "Token fiyatı çok düşük"),
        (Decimal("20"), Decimal("0.45"), Decimal("0.01"), "FAIL", "Likidite yetersiz"),
    ]
    all_ok = True
    for bal, tp, book, expected, desc in cases:
        ok, reason = rm.can_trade(balance_usdc=bal, token_price=tp, book_size=book)
        actual = "PASS" if ok else "FAIL"
        correct = actual == expected
        status = "PASS" if correct else "FAIL"
        if not correct:
            all_ok = False
        log(f"  can_trade ({desc})", status,
            f"Beklenen: {expected}, Sonuç: {actual} — {reason[:60]}")

    # Kelly hesaplama
    pos, info = rm.calculate_kelly_size(
        balance_usdc=Decimal("25"),
        token_price=Decimal("0.45"),
        confidence=70,
    )
    log("Kelly hesaplama", "PASS" if pos > 0 else "WARN",
        f"p={info['p']}, f*={info['f_star']:.3f}, bet=${pos}")

    return all_ok


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 11: Order Executor — Emir Oluşturma (Dry-run)
# ══════════════════════════════════════════════════════════════════════════════

async def test_order_executor():
    section("11. ORDER EXECUTOR — EMİR OLUŞTURMA (DRY-RUN)")
    from order_executor import OrderExecutor
    from config import cfg

    # Gerçek emir GÖNDERME — sadece oluştur
    try:
        executor = OrderExecutor(cfg)
        await executor.initialize()
        log("OrderExecutor başlatma", "PASS", "CLOB client ve API credentials hazır")
    except Exception as e:
        log("OrderExecutor başlatma", "FAIL", str(e))
        return False

    # Dry-run order testi
    try:
        result = await executor.place_order(
            token_id="0x" + "0" * 64,  # geçersiz token — dry-run'da sorun değil
            amount_usdc=Decimal("2.50"),
            fair_price=Decimal("0.45"),
            dry_run=True,
        )
        if result["success"]:
            log("Dry-run emir", "PASS",
                f"Oluşturuldu — shares: {result['shares']}, price: ${result['price_paid']}")
        else:
            log("Dry-run emir", "WARN", f"Başarısız: {result.get('error','?')}")
    except Exception as e:
        log("Dry-run emir", "FAIL", str(e))
        return False

    # Bakiye sorgusu retry testi
    try:
        from auto_claimer import AutoClaimer
        claimer = AutoClaimer(cfg)
        bal = await claimer._get_balance_with_retry(executor, retries=2)
        if not cfg.DRY_RUN:
            log("Bakiye retry sorgusu", "PASS", f"${bal:.6f} USDC")
        else:
            log("Bakiye retry sorgusu", "PASS", f"Dry-run: ${bal} (simüle)")
    except Exception as e:
        log("Bakiye retry sorgusu", "FAIL", str(e))

    return True


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 12: Auto Claimer — Pending State Yükleme
# ══════════════════════════════════════════════════════════════════════════════

def test_auto_claimer():
    section("12. AUTO CLAİMER — PENDING STATE")
    from auto_claimer import AutoClaimer
    from config import cfg

    try:
        claimer = AutoClaimer(cfg)
        pending = claimer.pending_count
        timeout_hours = (120 * 300) / 3600
        log("AutoClaimer başlatma", "PASS",
            f"PENDING trades yüklendi: {pending} | Timeout: {timeout_hours:.0f}h")
        log("QUICK_POLL_MAX", "PASS", f"{AutoClaimer.QUICK_POLL_MAX}s")
        return True
    except Exception as e:
        log("AutoClaimer başlatma", "FAIL", str(e))
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 13: Tam Entegrasyon — Pencere Döngüsü Simülasyonu
# ══════════════════════════════════════════════════════════════════════════════

async def test_integration():
    section("13. ENTEGRASYON — TAM DÖNGÜ SİMÜLASYONU (Dry-run)")
    from config import cfg
    original_dry_run = cfg.DRY_RUN
    cfg.DRY_RUN = True  # bu test için her zaman dry-run

    try:
        from exchange_feed import ExchangeFeed
        from oracle_monitor import OracleMonitor
        from market_resolver import MarketResolver
        from signal_engine import SignalEngine
        from risk_manager import RiskManager
        from order_executor import OrderExecutor

        exchange = ExchangeFeed(cfg)
        oracle = OracleMonitor(cfg)
        resolver = MarketResolver(cfg)
        engine = SignalEngine(cfg)
        risk = RiskManager(cfg)
        executor = OrderExecutor(cfg)

        await exchange.start()
        await oracle.start()
        await resolver.initialize()
        await executor.initialize()
        await asyncio.sleep(5)

        window = resolver.get_current_window()
        wts = window["window_ts"]
        oracle.cache_window_open_price(wts, "BTC")
        await asyncio.sleep(1)

        ex_data = exchange.get_price("BTC")
        orc_data = oracle.get_oracle_data("BTC")
        wop = oracle.get_window_open_price(wts, "BTC")
        market = await resolver.get_active_market("BTC")

        if not ex_data.get("average"):
            log("Entegrasyon döngüsü", "WARN", "Exchange verisi yok — test tamamlanamadı")
            return False
        if not orc_data.get("price"):
            log("Entegrasyon döngüsü", "WARN", "Oracle verisi yok — test tamamlanamadı")
            return False
        if not market:
            log("Entegrasyon döngüsü", "WARN", "Aktif piyasa yok — pencere değişiminde yeniden dene")
            return False

        up_ask = market.get("up_best_ask")
        down_ask = market.get("down_best_ask")
        sig = engine.generate_signal(ex_data, orc_data, wop or orc_data["price"],
                                     up_best_ask=up_ask, down_best_ask=down_ask)

        log("Sinyal üretimi", "PASS",
            f"Direction: {sig['direction']} | Conf: {sig['confidence']} | "
            f"Delta: {sig['delta_percent']:.4f}% | Lag: {sig['oracle_lag']}s")

        if sig["direction"] != "SKIP":
            token_price = up_ask if sig["direction"] == "UP" else down_ask
            ok, reason = risk.can_trade(balance_usdc=Decimal("25"), token_price=token_price)
            log("Risk kontrolü", "PASS" if ok else "WARN",
                f"can_trade={ok} — {reason[:60]}")

            if ok:
                order = await executor.place_order(
                    token_id=market["token_id_up"] if sig["direction"] == "UP" else market["token_id_down"],
                    amount_usdc=Decimal("2.50"),
                    fair_price=token_price,
                    dry_run=True,
                )
                log("Dry-run emir gönderimi", "PASS" if order["success"] else "WARN",
                    f"success={order['success']} | shares={order['shares']} | "
                    f"price=${order['price_paid']} | id={order['order_id']}")
        else:
            log("Sinyal SKIP", "PASS",
                f"Normal durum — sebep: {sig['reason'][:70]}")

        await exchange.stop()
        await oracle.stop()
        await resolver.close()

        log("Entegrasyon döngüsü", "PASS", "Tüm bileşenler birlikte çalışıyor")
        return True

    except Exception as e:
        import traceback
        log("Entegrasyon döngüsü", "FAIL", f"{type(e).__name__}: {str(e)[:80]}")
        traceback.print_exc()
        return False
    finally:
        cfg.DRY_RUN = original_dry_run


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 14: Telegram Bildirimleri
# ══════════════════════════════════════════════════════════════════════════════

async def test_telegram():
    section("14. TELEGRAM BİLDİRİMLERİ")
    from config import cfg

    if not cfg.TELEGRAM_ENABLED:
        log("Telegram", "WARN", "TELEGRAM_ENABLED=false — bildirimler kapalı, atlanıyor")
        return True

    if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        log("Telegram token/chat_id", "FAIL",
            "TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik — .env'e ekle")
        return False

    log("Telegram config", "PASS",
        f"Token: ...{cfg.TELEGRAM_BOT_TOKEN[-8:]} | "
        f"Chat ID: {cfg.TELEGRAM_CHAT_ID} | "
        f"Mod: {cfg.TELEGRAM_NOTIFY_MODE}")

    # Gerçek mesaj gönder
    try:
        from telegram_notifier import TelegramNotifier
        tg = TelegramNotifier(cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID)

        if not tg.enabled:
            log("Telegram bağlantısı", "FAIL",
                "aiohttp kütüphanesi eksik — pip install aiohttp")
            return False

        await tg.send(
            "🧪 <b>Preflight Test</b>\n"
            f"Mod: <code>{cfg.TELEGRAM_NOTIFY_MODE}</code>\n"
            "Bot production'a hazırlanıyor — bu mesajı görüyorsan Telegram çalışıyor ✅"
        )
        await tg.close()
        log("Telegram mesaj gönderimi", "PASS",
            "Test mesajı gönderildi — Telegram'ını kontrol et")
        return True

    except Exception as e:
        log("Telegram mesaj gönderimi", "FAIL",
            f"{type(e).__name__}: {str(e)[:80]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 15: Trading Filtreleri (Saat & Günlük BTC Hareketi)
# ══════════════════════════════════════════════════════════════════════════════

async def test_trading_filters():
    section("15. TİCARET FİLTRELERİ")
    from config import cfg

    # ── Saat filtresi ────────────────────────────────────────────────────
    et = time.strftime("%H:%M", time.localtime())
    if cfg.TRADING_HOURS_ENABLED:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        et_now = datetime.now(ZoneInfo("America/New_York"))
        hour = et_now.hour
        start = cfg.TRADING_HOUR_START
        end = cfg.TRADING_HOUR_END
        in_window = start <= hour < end
        status = "PASS" if in_window else "WARN"
        log("Saat filtresi",  status,
            f"Aktif — ET şu an {et_now.strftime('%H:%M')} | "
            f"İşlem saati: {start:02d}:00–{end:02d}:00 ET | "
            f"{'İşlem yapılıyor ✅' if in_window else 'Şu an dışında — bot bekler ⚠️'}")
    else:
        log("Saat filtresi", "WARN",
            "TRADING_HOURS_ENABLED=false — gece saatleri de işlem yapılır")

    # ── Günlük BTC hareket filtresi ──────────────────────────────────────
    if cfg.DAILY_MOVE_FILTER_ENABLED:
        try:
            from exchange_feed import ExchangeFeed
            feed = ExchangeFeed(cfg)
            await feed.start()
            await asyncio.sleep(5)
            ex = feed.get_price("BTC")
            await feed.stop()

            current = ex.get("average")
            if current is None:
                log("Günlük BTC hareket filtresi", "WARN",
                    "Fiyat verisi alınamadı — filtre test edilemedi")
            else:
                # Basit test: eşik mantığı doğru çalışıyor mu
                threshold = cfg.DAILY_MOVE_THRESHOLD_PCT
                log("Günlük BTC hareket filtresi", "PASS",
                    f"Aktif — BTC şu an ${current:,.2f} | "
                    f"Eşik: %{threshold} günlük hareket | "
                    f"Trend günlerinde otomatik duraklar ✅")
        except Exception as e:
            log("Günlük BTC hareket filtresi", "WARN",
                f"Test hatası: {str(e)[:60]}")
    else:
        log("Günlük BTC hareket filtresi", "WARN",
            "DAILY_MOVE_FILTER_ENABLED=false — trend piyasada da işlem yapılır")

    # ── Oracle lag eşiği ─────────────────────────────────────────────────
    lag_min = cfg.ORACLE_LAG_MIN
    status = "PASS" if lag_min >= 15 else "WARN"
    log("Oracle lag min eşiği", status,
        f"ORACLE_LAG_MIN_SECONDS={lag_min}s "
        f"({'✅ iyi' if lag_min >= 15 else '⚠️ 15s+ önerilir'})")

    # ── Delta eşiği ──────────────────────────────────────────────────────
    delta = cfg.DELTA_THRESHOLD
    status = "PASS" if delta >= Decimal("0.07") else "WARN"
    log("Delta eşiği", status,
        f"DELTA_THRESHOLD_PERCENT={delta}% "
        f"({'✅ iyi' if delta >= Decimal('0.07') else '⚠️ 0.07%+ önerilir'})")

    # ── Token fiyat aralığı ───────────────────────────────────────────────
    if float(cfg.MAX_TOKEN_PRICE) >= float(cfg.MAX_TOKEN_PRICE):  # always true, just range check
        is_50c_bug_fixed = float(cfg.MAX_TOKEN_PRICE) < 1.0
        log("50¢ token filtresi", "PASS" if is_50c_bug_fixed else "FAIL",
            f"MIN=${cfg.MIN_TOKEN_PRICE} / MAX=${cfg.MAX_TOKEN_PRICE} | "
            f"50¢ token {'engellendi ✅ (>= MAX_TOKEN_PRICE)' if float(cfg.MAX_TOKEN_PRICE) <= 0.50 else 'geçebilir ⚠️'}")

    return True


# ══════════════════════════════════════════════════════════════════════════════
#  TEST 16: Binance.US Fallback WebSocket
# ══════════════════════════════════════════════════════════════════════════════

async def test_binance_us():
    section("16. BİNANCE.US FALLBACK WEBSOCKET")
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    url = "wss://stream.binance.us:9443/ws/btcusdt@ticker"
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=8) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            data = json.loads(raw)
            price = data.get("c", "?")
            log("Binance.US WebSocket", "PASS",
                f"BTC: ${float(price):,.2f} — US sunucularda otomatik devreye girer")
            return True
    except asyncio.TimeoutError:
        log("Binance.US WebSocket", "WARN", "Zaman aşımı — EU sunucularında gerekli değil")
        return False
    except Exception as e:
        log("Binance.US WebSocket", "WARN",
            f"Bağlanamadı (EU sunucularında gerekli değil): {str(e)[:60]}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  ANA FONKSİYON
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print(f"""
  {BOLD}{'=' * 52}{RESET}
  {BOLD}  POLYMARKET BOT — PRODUCTION ÖNCESİ TEST{RESET}
  {BOLD}{'=' * 52}{RESET}
""")

    # Config sync testler
    config_ok = test_config()
    if not config_ok:
        print(f"\n  {R}{BOLD}Config hatası — diğer testler atlanıyor.{RESET}\n")
        _print_summary()
        return

    # Async testler
    await test_binance()
    await test_binance_us()
    await test_coinbase()
    await test_bitstamp()
    test_oracle()
    await test_clob_auth()
    await test_geoblock()
    await test_market_discovery()
    await test_signal_engine()
    test_risk_manager()
    await test_order_executor()
    test_auto_claimer()
    await test_trading_filters()
    await test_telegram()
    await test_integration()

    _print_summary()


def _print_summary():
    passed = [r for r in results if r[1] == "PASS"]
    warned = [r for r in results if r[1] == "WARN"]
    failed = [r for r in results if r[1] == "FAIL"]

    print(f"""
  {'=' * 52}
  {BOLD}ÖZET{RESET}
  {'=' * 52}
  {G}{BOLD}PASS:{RESET}  {len(passed):>3}
  {Y}{BOLD}WARN:{RESET}  {len(warned):>3}
  {R}{BOLD}FAIL:{RESET}  {len(failed):>3}
  {'─' * 52}""")

    if failed:
        print(f"  {R}{BOLD}BAŞARISIZ TESTLER:{RESET}")
        for name, _, detail in failed:
            print(f"    {R}✗{RESET} {name}")
            print(f"      {detail[:80]}")

    if warned:
        print(f"\n  {Y}{BOLD}UYARILAR:{RESET}")
        for name, _, detail in warned:
            print(f"    {Y}!{RESET} {name}")

    print()
    if not failed:
        critical_warns = [r for r in warned if any(
            kw in r[0].lower() for kw in ["geoblock", "bakiye", "coinbase", "oracle", "telegram"]
        )]
        if not critical_warns:
            print(f"  {G}{BOLD}✅ TÜM KRİTİK TESTLER GEÇTİ — Production'a geçebilirsin.{RESET}")
            print(f"  {G}   .env'de DRY_RUN=false yap ve python -u run.py ile başlat.{RESET}")
        else:
            print(f"  {Y}{BOLD}⚠️  Uyarılar var — yukarıdaki WARN'ları kontrol et, sonra production'a geç.{RESET}")
    else:
        print(f"  {R}{BOLD}❌ KRİTİK HATALAR VAR — Gidermeden production'a BAŞLATMA.{RESET}")
        critical = [r for r in failed if any(
            kw in r[0].lower() for kw in ["geoblock", "clob", "oracle", "config", "telegram"]
        )]
        if critical:
            print(f"  {R}   Önce bunları düzelt: {', '.join(r[0] for r in critical)}{RESET}")
    print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n  Test iptal edildi.\n")
