"""
order_executor.py — Emir Göndericisi
======================================
GTC limit emri (fair x 1.40, tavan cfg.MAX_TOKEN_PRICE). Emir teminatı amount_usdc'yi asla gecmez.
"""

import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY

from config import cfg


class OrderExecutor:
    """Polymarket CLOB API'ye emir gönderir."""

    MIN_SHARES = 5

    def estimate_limit_price(self, fair_price: Decimal) -> Decimal:
        """GTC limit — risk_manager ile aynı: fair × 1.40, tavan MAX_TOKEN_PRICE."""
        cap = self._cfg.MAX_TOKEN_PRICE
        if not fair_price or fair_price <= 0:
            return cap
        lp = (fair_price * Decimal("1.40")).quantize(
            Decimal("0.01"), rounding=ROUND_DOWN
        )
        return min(lp, cap)

    def __init__(self, config=None):
        self._cfg = config or cfg
        self._clob: Optional[ClobClient] = None

    # ── Başlatma ─────────────────────────────────────────────────────────

    async def initialize(self):
        """CLOB client kur ve API kimlik bilgilerini türet."""
        self._clob = ClobClient(
            host=self._cfg.CLOB_HOST,
            key=self._cfg.PRIVATE_KEY,
            chain_id=self._cfg.CHAIN_ID,
            signature_type=self._cfg.SIGNATURE_TYPE,
            funder=self._cfg.FUNDER_ADDRESS,
        )
        self._clob.set_api_creds(self._clob.create_or_derive_api_creds())

    # ── Bakiye Sorgulama ─────────────────────────────────────────────────

    USDC_DECIMALS = Decimal("1000000")

    async def get_balance(self) -> Decimal:
        """Cüzdandaki USDC bakiyesini sorgula."""
        if self._cfg.DRY_RUN:
            return Decimal("1000")
        if self._clob is None:
            return Decimal("0")

        try:
            loop = asyncio.get_running_loop()
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            balance = await loop.run_in_executor(
                None, self._clob.get_balance_allowance, params
            )
            raw = Decimal("0")
            if hasattr(balance, "balance"):
                raw = Decimal(str(balance.balance))
            elif isinstance(balance, dict):
                raw = Decimal(str(balance.get("balance", "0")))

            if raw > Decimal("1000"):
                return (raw / self.USDC_DECIMALS).quantize(Decimal("0.000001"))
            return raw
        except Exception as e:
            from logger_module import log_error
            log_error(f"Bakiye sorgulama hatasi: {e}")
            return Decimal("0")

    # ── Emir Gönderme ───────────────────────────────────────────────────

    async def place_order(
        self,
        token_id: str,
        amount_usdc: Decimal,
        dry_run: bool = None,
        fair_price: Decimal = None,
        use_market_order: bool = False,
    ) -> dict:
        """GTC limit alim. Teminat <= amount_usdc (API bakiye hatasini onler)."""
        if dry_run is None:
            dry_run = self._cfg.DRY_RUN

        if self._clob is None:
            return {
                "success": False, "order_id": None, "token_id": token_id,
                "price_paid": Decimal("0"), "shares": Decimal("0"),
                "total_cost": amount_usdc, "dry_run": dry_run,
                "error": "CLOB client henuz baslatilmadi",
                "error_type": "pre_send", "order_type_used": "NONE",
            }

        result = {
            "success": False, "order_id": None, "token_id": token_id,
            "price_paid": Decimal("0"), "shares": Decimal("0"),
            "total_cost": amount_usdc, "dry_run": dry_run,
            "error": None, "order_type_used": "GTC",
        }

        try:
            loop = asyncio.get_running_loop()

            # ── Referans fiyat: fair_price (Gamma) HER ZAMAN tercih edilir ─
            # CLOB order book $0.95+ gösteriyor, Gamma gerçek fair value
            ref_price = None
            if fair_price and fair_price > 0:
                ref_price = fair_price
            else:
                book = await loop.run_in_executor(
                    None, self._clob.get_order_book, token_id
                )
                if book and hasattr(book, "asks") and book.asks:
                    ref_price = Decimal(str(book.asks[0].price))

            if ref_price is None or ref_price <= 0:
                result["error"] = "Fair_price ve order book bos"
                result["error_type"] = "pre_send"
                return result

            max_tp = self._cfg.MAX_TOKEN_PRICE
            if ref_price > max_tp:
                result["error"] = (
                    f"Referans token fiyati ${ref_price} > max ${max_tp} — emir yok"
                )
                result["error_type"] = "pre_send"
                return result

            result["price_paid"] = ref_price

            # ── DRY RUN ───────────────────────────────────────────────
            if dry_run:
                shares = (amount_usdc / ref_price).quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN
                )
                if shares < self.MIN_SHARES:
                    shares = Decimal(str(self.MIN_SHARES))
                result["shares"] = shares
                result["success"] = True
                result["order_id"] = "DRY-RUN-SIMULATED"
                return result

            # ── GTC: limit fiyat, pay sayısı teminatı asla amount_usdc'yi gecmesin ─
            limit_price = self.estimate_limit_price(ref_price)
            if limit_price > max_tp:
                result["error"] = (
                    f"Limit fiyat ${limit_price} > max ${max_tp} — emir yok"
                )
                result["error_type"] = "pre_send"
                return result

            min_collateral = self.MIN_SHARES * limit_price

            if amount_usdc < min_collateral:
                result["error"] = (
                    f"Min emir tutari: ${amount_usdc} < "
                    f"${min_collateral.quantize(Decimal('0.01'))} "
                    f"(5 share x ${limit_price})"
                )
                result["error_type"] = "pre_send"
                return result

            budget = (amount_usdc * Decimal("0.998")).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN
            )
            gtc_shares = (budget / limit_price).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            if gtc_shares < self.MIN_SHARES:
                gtc_shares = Decimal(str(self.MIN_SHARES))

            notional = (gtc_shares * limit_price).quantize(
                Decimal("0.000001"), rounding=ROUND_DOWN
            )
            if notional > amount_usdc:
                gtc_shares = (amount_usdc / limit_price).quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN
                )
            if gtc_shares < self.MIN_SHARES:
                result["error"] = (
                    f"Emir teminati sigmiyor: ${amount_usdc} ile "
                    f"min {self.MIN_SHARES} share mumkun degil (limit ${limit_price})"
                )
                result["error_type"] = "pre_send"
                return result

            notional = (gtc_shares * limit_price).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            if notional > amount_usdc:
                result["error"] = (
                    f"Emir tutari tasmasi: ${notional} > ${amount_usdc}"
                )
                result["error_type"] = "pre_send"
                return result

            result["shares"] = gtc_shares
            result["price_paid"] = limit_price

            try:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=float(limit_price),
                    size=float(gtc_shares),
                    side=BUY,
                )
                signed_order = self._clob.create_order(order_args)
            except Exception as e_create:
                result["error"] = f"Emir olusturulamadi: {e_create}"
                result["error_type"] = "pre_send"
                return result

            try:
                response = await loop.run_in_executor(
                    None, self._clob.post_order, signed_order, OrderType.GTC
                )
            except Exception as post_err:
                result["error"] = f"Emir gonderme hatasi: {post_err}"
                result["error_type"] = "post_send"
                return result

            if response:
                order_id, success = self._parse_response(response)
                if success:
                    result["success"] = True
                    result["order_id"] = order_id
                    result["order_type_used"] = "GTC"
                    return result

            result["error"] = "API bos yanit dondurdu"
            result["error_type"] = "post_send"

        except Exception as e:
            error_msg = str(e)
            result["error_type"] = "post_send"
            low = error_msg.lower()
            if "insufficient" in low or "not enough balance" in low:
                result["error"] = f"Bakiye yetersiz: {error_msg}"
                result["error_type"] = "pre_send"
            elif "rejected" in low or "fill" in low:
                result["error"] = f"Emir reddedildi: {error_msg}"
            else:
                result["error"] = f"API hatasi: {error_msg}"

        return result

    # ── Yanıt Parse ───────────────────────────────────────────────────

    @staticmethod
    def _parse_response(response) -> tuple:
        """API yanıtından (order_id, success) çıkar."""
        if isinstance(response, dict):
            order_id = response.get("orderID", response.get("id", "unknown"))
            success = response.get("success", True)
        else:
            order_id = str(response)
            success = True
        return order_id, success

    # ── Açık Emirleri Sorgula / İptal Et ─────────────────────────────────

    async def get_open_orders(self) -> list:
        """Açık emirleri listele."""
        if self._clob is None:
            return []
        try:
            loop = asyncio.get_running_loop()
            orders = await loop.run_in_executor(None, self._clob.get_orders)
            if orders:
                return orders if isinstance(orders, list) else [orders]
        except Exception as e:
            from logger_module import log_error
            log_error(f"Emir sorgulama hatasi: {e}")
        return []

    async def get_positions(self, redeemable_only: bool = False) -> list:
        """Data API üzerinden açık pozisyonları sorgula."""
        try:
            import aiohttp, ssl, certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            conn = aiohttp.TCPConnector(ssl=ssl_ctx)
            url = "https://data-api.polymarket.com/positions"
            params = {
                "user": self._cfg.FUNDER_ADDRESS,
                "sizeThreshold": "0",
            }
            if redeemable_only:
                params["redeemable"] = "true"
            async with aiohttp.ClientSession(connector=conn) as s:
                async with s.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data if isinstance(data, list) else []
        except Exception as e:
            from logger_module import log_error
            log_error(f"Pozisyon sorgulama hatasi: {e}")
        return []

    async def cancel_all_orders(self) -> bool:
        """Tüm açık emirleri iptal et. Shutdown ve pencere geçişlerinde çağrılır."""
        if self._cfg.DRY_RUN:
            return True
        if self._clob is None:
            return True
        try:
            loop = asyncio.get_running_loop()
            orders = await self.get_open_orders()
            if not orders:
                return True

            order_ids = []
            for o in orders:
                oid = o.get("id") if isinstance(o, dict) else getattr(o, "id", None)
                if oid:
                    order_ids.append(oid)

            if order_ids:
                await loop.run_in_executor(
                    None, self._clob.cancel_orders, order_ids
                )
            return True
        except Exception as e:
            from logger_module import log_error
            log_error(f"Emir iptal hatasi: {e}")
            return False
