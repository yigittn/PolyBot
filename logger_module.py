"""
logger_module.py — Loglama Sistemi
=====================================
Her işlemi JSON dosyasına (data/trades.jsonl) ve konsola yazar.

Konsol çıktısı renkli ve okunabilir, JSON dosyası makine tarafından okunabilir.
Her satır bağımsız bir JSON kaydıdır (JSONL format).

Kullanım:
    from logger_module import log_trade, log_skip, log_error, print_summary
    log_trade({...})
    log_skip("delta yetersiz", window_ts=1743173700)
"""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from config import cfg


# ── JSON serializer (Decimal desteği) ────────────────────────────────────

def _json_default(obj):
    """Decimal ve datetime nesnelerini JSON'a çevirir."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


# ── Dosyaya Yazma ────────────────────────────────────────────────────────

def _ensure_log_dir():
    """Log dosyasının dizininin var olduğundan emin ol."""
    log_path = Path(cfg.PROJECT_ROOT) / cfg.LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path


def _write_jsonl(data: dict):
    """data/trades.jsonl dosyasına bir satır JSON ekle."""
    log_path = _ensure_log_dir()
    line = json.dumps(data, default=_json_default, ensure_ascii=False)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Konsol Renkleri ──────────────────────────────────────────────────────

class _C:
    """ANSI renk kodları."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def _now_str() -> str:
    """Şu anki zamanı [HH:MM:SS] formatında döndür."""
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ── Log Fonksiyonları ───────────────────────────────────────────────────

def log_trade(trade_data: dict):
    """
    İşlem detaylarını dosyaya ve konsola yaz.

    Beklenen alanlar:
        timestamp, window_ts, direction, exchange_price, oracle_price,
        window_open_price, delta_percent, oracle_lag_seconds, confidence,
        token_price_paid, shares, total_cost_usdc, dry_run

    Not: Dry-run işlemleri data/trades.jsonl'e yazılmaz (sadece gerçek emirler).
    """
    trade_data["event"] = "TRADE"
    trade_data.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    if not trade_data.get("dry_run") and not cfg.DRY_RUN:
        _write_jsonl(trade_data)

    # Konsola yaz
    direction = trade_data.get("direction", "?")
    delta = trade_data.get("delta_percent", "?")
    lag = trade_data.get("oracle_lag_seconds", "?")
    conf = trade_data.get("confidence", "?")
    price = trade_data.get("token_price_paid", "?")
    shares = trade_data.get("shares", "?")
    cost = trade_data.get("total_cost_usdc", "?")
    dry = " [DRY]" if trade_data.get("dry_run") else ""

    if direction == "UP":
        color = _C.GREEN
        icon = "UP "
    else:
        color = _C.RED
        icon = "DOWN"

    print(
        f"  [{_now_str()}] {color}{icon}{_C.RESET} | "
        f"D {'+' if str(delta).startswith('-') == False else ''}{delta}% | "
        f"Lag: {lag}s | Conf: {conf} | "
        f"${price} x {shares} = ${cost}{dry}"
    )


def log_result(result_data: dict):
    """
    İşlem sonucunu (WIN/LOSS) dosyaya ve konsola yaz.

    Beklenen alanlar:
        result, payout_usdc, profit_usdc, daily_pnl,
        wins, losses, win_rate, consecutive_losses

    Not: Dry-run modunda dosyaya yazılmaz; böylece trades.jsonl yalnızca
    production W/L ve istatistikleri içerir.
    """
    result_data["event"] = "RESULT"
    result_data.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    result_data.setdefault("dry_run", cfg.DRY_RUN)
    if not cfg.DRY_RUN:
        _write_jsonl(result_data)

    result = result_data.get("result", "?")
    profit = result_data.get("profit_usdc", "0")
    daily = result_data.get("daily_pnl", "0")
    wins = result_data.get("wins", 0)
    losses = result_data.get("losses", 0)
    rate = result_data.get("win_rate", "0")

    if result == "WIN":
        print(
            f"  [{_now_str()}] {_C.GREEN}{_C.BOLD}WIN{_C.RESET} "
            f"+${profit} | Gun: ${daily} | "
            f"{wins}W/{losses}L (%{rate})"
        )
    else:
        print(
            f"  [{_now_str()}] {_C.RED}{_C.BOLD}LOSS{_C.RESET} "
            f"-${abs(Decimal(str(profit)))} | Gun: ${daily} | "
            f"{wins}W/{losses}L (%{rate})"
        )


def log_skip(reason: str, window_ts: int = 0):
    """
    Atlanan işlemi konsola yaz (dosyaya isteğe bağlı).

    Parametreler:
        reason:    Atlanma sebebi
        window_ts: İlgili pencere timestamp'i
    """
    # SKIP sadece konsolda (trades.jsonl production odaklı kalsın)
    print(f"  [{_now_str()}] {_C.GRAY}SKIP: {reason}{_C.RESET}")


def log_cooldown(consecutive_losses: int, remaining_windows: int):
    """Cooldown durumunu konsola yaz."""
    print(
        f"  [{_now_str()}] {_C.YELLOW}{_C.BOLD}COOLDOWN{_C.RESET}: "
        f"{consecutive_losses} ardisik kayip, "
        f"{remaining_windows} pencere bekleniyor"
    )


def log_error(error: str, context: dict = None):
    """
    Hata logla — hem dosyaya hem konsola.

    Parametreler:
        error:   Hata mesajı
        context: Ek bağlam bilgisi (dict)
    """
    data = {
        "event": "ERROR",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "context": context or {},
    }
    if not cfg.DRY_RUN:
        _write_jsonl(data)

    print(f"  [{_now_str()}] {_C.RED}HATA: {error}{_C.RESET}")


def print_summary(stats: dict):
    """
    Günlük özet yazdır.

    Beklenen alanlar:
        daily_pnl, total_trades, wins, losses, win_rate, consecutive_losses
    """
    pnl = stats.get("daily_pnl", "0")
    trades = stats.get("total_trades", 0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    rate = stats.get("win_rate", "0")

    pnl_color = _C.GREEN if Decimal(str(pnl)) >= 0 else _C.RED

    print(f"""
  +---------- GUNLUK OZET ----------+
  |  PnL:       {pnl_color}${pnl}{_C.RESET}
  |  Islem:     {trades} ({wins}W / {losses}L)
  |  Basari:    %{rate}
  +---------------------------------+
""")


def print_dry_run_report(stats: dict, verdict: str):
    """
    Dry-run sonuç raporu yazdır ve dosyaya kaydet.

    Beklenen alanlar (get_stats() çıktısı):
        daily_pnl, total_trades, wins, losses, win_rate,
        total_windows, total_skips, max_consecutive_losses,
        max_consecutive_wins, start_time
    """
    total_windows = stats.get("total_windows", 0)
    total_trades = stats.get("total_trades", 0)
    total_skips = stats.get("total_skips", 0)
    wins = stats.get("wins", 0)
    losses = stats.get("losses", 0)
    win_rate = stats.get("win_rate", "0")
    pnl = stats.get("daily_pnl", "0")
    max_loss_streak = stats.get("max_consecutive_losses", 0)
    max_win_streak = stats.get("max_consecutive_wins", 0)
    start_time = stats.get("start_time", datetime.now(timezone.utc))

    # İşlem oranı
    trade_pct = "0"
    if total_windows > 0:
        trade_pct = f"{(total_trades / total_windows * 100):.1f}"

    # Ortalama kâr/işlem
    avg_profit = "0"
    if total_trades > 0:
        avg_profit = f"{Decimal(str(pnl)) / Decimal(total_trades):.2f}"

    # Çalışma süresi
    elapsed = datetime.now(timezone.utc) - start_time
    hours = elapsed.total_seconds() / 3600

    pnl_color = _C.GREEN if Decimal(str(pnl)) >= 0 else _C.RED
    wr_color = _C.GREEN if float(str(win_rate)) >= 55 else _C.RED

    report = f"""
  {_C.BOLD}===  DRY-RUN OZET ({hours:.1f} saat)  ==={_C.RESET}
  Toplam pencere:        {total_windows}
  Islem yapilan:         {total_trades} (%{trade_pct} — geri kalan SKIP)
  Atlanan (SKIP):        {total_skips}
  Kazanan:               {_C.GREEN}{wins}{_C.RESET} ({wr_color}%{win_rate} win rate{_C.RESET})
  Kaybeden:              {_C.RED}{losses}{_C.RESET}
  Simule PnL:            {pnl_color}${pnl}{_C.RESET}
  Ortalama kar/islem:    ${avg_profit}
  En buyuk kayip serisi: {max_loss_streak}
  En buyuk kazanc serisi:{max_win_streak}
  {_C.BOLD}=================================={_C.RESET}

  {_C.BOLD}DEGERLENDIRME:{_C.RESET}
  {verdict}

  Referans:
    Win rate <%55  -> GERCEK PARAYLA CALISTIRMA
    Win rate %55-60 -> Parametreleri ayarla
    Win rate %60-65 -> Kucuk miktarla ($50) basla
    Win rate >%65  -> Pozisyon boyutunu artirabilirsin
"""
    print(report)
    # Özet sadece konsolda; trades.jsonl yalnızca production TRADE/RESULT için kullanılır.
