"""
telegram_notifier.py — Telegram Bildirim Modülü
================================================
Bot işlemlerini ve uyarıları Telegram üzerinden bildirir.

Kurulum:
  1. @BotFather'dan yeni bot oluştur → BOT_TOKEN al
  2. Bota bir mesaj gönder, sonra @userinfobot'tan CHAT_ID al
  3. .env'e ekle:
       TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
       TELEGRAM_CHAT_ID=987654321

Token yoksa tüm çağrılar sessizce atlanır (bot çalışmaya devam eder).

Hangi olaylarda mesaj gideceği: .env içinde TELEGRAM_NOTIFY_MODE
(all | results | critical). Ayrıntı run.py içinde _telegram_should_notify.
"""

import asyncio
import ssl
import time
from decimal import Decimal
from typing import Optional

try:
    import aiohttp
    import certifi
    _AIOHTTP_OK = True
except ImportError:
    _AIOHTTP_OK = False
    certifi = None  # type: ignore


class TelegramNotifier:
    """Telegram Bot API üzerinden mesaj gönderir."""

    _BASE = "https://api.telegram.org/bot{token}/sendMessage"
    _TIMEOUT = 8  # saniye

    def __init__(self, token: str = "", chat_id: str = ""):
        self._token = token.strip()
        self._chat_id = chat_id.strip()
        self._enabled = bool(self._token and self._chat_id and _AIOHTTP_OK)
        self._session: Optional[aiohttp.ClientSession] = None
        # Rate limiting: max 1 mesaj / 3 saniye
        self._last_sent: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _get_session(self) -> "aiohttp.ClientSession":
        if self._session is None or self._session.closed:
            # Sistem CA eksik / kurumsal proxy: certifi ile doğrula
            ssl_ctx = None
            if certifi is not None:
                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx) if ssl_ctx else None
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self._TIMEOUT),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, text: str, silent: bool = False):
        """Ham mesaj gönder. Hata olursa sessizce atla."""
        if not self._enabled:
            return

        # Rate limit
        now = time.monotonic()
        if now - self._last_sent < 3.0:
            await asyncio.sleep(3.0 - (now - self._last_sent))
        self._last_sent = time.monotonic()

        url = self._BASE.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"  [Telegram] Hata {resp.status}: {body[:120]}")
        except Exception as e:
            print(f"  [Telegram] Gonderim hatasi: {e}")

    # ── Hazır Şablonlar ──────────────────────────────────────────────────

    async def notify_startup(self, balance: Decimal, mode: str = "PRODUCTION"):
        await self.send(
            f"🤖 <b>Bot Başladı</b>\n"
            f"Mod: <code>{mode}</code>\n"
            f"Bakiye: <b>${balance}</b> USDC",
            silent=True,
        )

    async def notify_trade_entry(
        self,
        asset: str,
        direction: str,
        token_price: Decimal,
        position_usdc: Decimal,
        confidence: int,
        delta_pct: Decimal,
        oracle_lag: int,
        trade_num: int,
    ):
        arrow = "📈" if direction == "UP" else "📉"
        await self.send(
            f"{arrow} <b>EMİR #{trade_num} — {asset} {direction}</b>\n"
            f"Token: <b>${token_price}</b> | Miktar: <b>${position_usdc}</b>\n"
            f"Confidence: <b>{confidence}</b> | Delta: <b>{delta_pct:+.3f}%</b>\n"
            f"Oracle lag: <b>{oracle_lag}s</b>"
        )

    async def notify_result(
        self,
        result: str,
        asset: str,
        direction: str,
        cost_usdc: Decimal,
        profit: Decimal,
        daily_pnl: Decimal,
        wins: int,
        losses: int,
        trade_num: int,
        reconciled: bool = False,
    ):
        total = wins + losses
        win_rate = int(wins / total * 100) if total > 0 else 0
        recon_tag = " (geç uzlaştırma)" if reconciled else ""

        if result == "WIN":
            emoji = "✅"
            pnl_line = f"Kar: <b>+${profit:.2f}</b>"
        else:
            emoji = "❌"
            pnl_line = f"Kayıp: <b>-${abs(cost_usdc):.2f}</b>"

        await self.send(
            f"{emoji} <b>SONUÇ #{trade_num}{recon_tag}: {result}</b>\n"
            f"{asset} {direction} | {pnl_line}\n"
            f"Günlük P&L: <b>${daily_pnl:+.2f}</b> | "
            f"W/L: <b>{wins}/{losses}</b> (%{win_rate})"
        )

    async def notify_drawdown_warning(
        self, daily_pnl: Decimal, daily_limit: Decimal
    ):
        pct = abs(daily_pnl) / daily_limit * 100
        await self.send(
            f"⚠️ <b>DRAWDOWN UYARISI</b>\n"
            f"Günlük P&L: <b>${daily_pnl:.2f}</b>\n"
            f"Limitin %{pct:.0f}'ine ulaşıldı (limit: -${daily_limit})"
        )

    async def notify_emergency_stop(self, reason: str):
        await self.send(
            f"🚨 <b>ACİL DURDURMA</b>\n"
            f"Sebep: <code>{reason}</code>\n"
            f"Bot durduruldu — manuel müdahale gerekli."
        )

    async def notify_shutdown(
        self, daily_pnl: Decimal, wins: int, losses: int, net_change: str
    ):
        total = wins + losses
        win_rate = int(wins / total * 100) if total > 0 else 0
        await self.send(
            f"🔴 <b>Bot Durdu</b>\n"
            f"Günlük P&L: <b>${daily_pnl:+.2f}</b>\n"
            f"İşlem: {total} ({wins}W/{losses}L, %{win_rate})\n"
            f"Net: <b>{net_change}</b>",
            silent=True,
        )

    async def notify_consecutive_losses(self, count: int, cooldown_windows: int):
        await self.send(
            f"⚠️ <b>{count} Ardışık Kayıp</b>\n"
            f"Cooldown: {cooldown_windows} pencere ({cooldown_windows * 5} dakika)"
        )
