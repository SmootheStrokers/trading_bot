"""
executor.py — Places orders on the Polymarket CLOB.
Handles order signing (EIP-712), slippage checks, and order confirmation.
Uses py-clob-client for live orders (EIP-712 signing required by Polymarket).
"""

import asyncio
import logging
import time
from typing import Optional, Tuple

from config import BotConfig
from clob_client import ClobClient
from models import Market, EdgeResult, Side

logger = logging.getLogger("executor")


def _make_py_clob_client(config: BotConfig):
    """Create the official py-clob-client for EIP-712 signed order placement."""
    from py_clob_client.client import ClobClient as PyClobClient
    from py_clob_client.clob_types import ApiCreds

    creds = ApiCreds(
        api_key=config.API_KEY,
        api_secret=config.API_SECRET,
        api_passphrase=config.API_PASSPHRASE,
    )
    # EOA: signature_type=0. Polymarket proxy: signature_type=1, funder=proxy wallet.
    sig_type = 1 if getattr(config, "PROXY_WALLET", None) else 0
    funder = getattr(config, "PROXY_WALLET", None) or None
    host = config.CLOB_API_URL.rstrip("/")
    return PyClobClient(
        host=host,
        chain_id=config.CHAIN_ID,
        key=config.PRIVATE_KEY,
        creds=creds,
        signature_type=sig_type,
        funder=funder,
    )


class OrderExecutor:
    def __init__(self, config: BotConfig, client: ClobClient):
        self.config = config
        self.client = client
        self._py_clob: Optional[object] = None

    def _get_py_clob(self):
        if self._py_clob is None:
            self._py_clob = _make_py_clob_client(self.config)
        return self._py_clob

    async def place_order(self, market: Market, edge: EdgeResult) -> Optional[str]:
        """
        Place a limit order for the given market and edge result.
        Returns the order ID if successful, None if failed.
        """
        token_id = (
            market.yes_token_id if edge.side == Side.YES
            else market.no_token_id
        )

        # Compute limit price with slippage tolerance
        entry_price = edge.entry_price
        if edge.side == Side.NO:
            entry_price = 1.0 - entry_price

        # Limit price = slightly above best ask to get filled, but within slippage
        limit_price = self._compute_limit_price(market, edge)
        if limit_price is None:
            logger.warning("Could not compute limit price — skipping order")
            return None

        shares = round(edge.kelly_size / limit_price, 4)

        logger.info(
            f"Placing order: {edge.side.value} {shares:.4f} shares @ {limit_price:.4f} "
            f"(${edge.kelly_size:.2f} USDC) | {market.question[:50]}"
        )

        if self.config.PAPER_TRADING or getattr(self.config, "DRY_RUN", False):
            order_id = f"paper-{int(time.time() * 1000)}"
            logger.info(f"PAPER/DRY_RUN: Simulated order OK | ID: {order_id} (no real order placed)")
            return order_id

        try:
            from py_clob_client.clob_types import OrderArgs

            py_client = self._get_py_clob()
            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side="BUY",
            )
            # py-clob-client is sync; run in thread to avoid blocking event loop
            resp = await asyncio.to_thread(
                py_client.create_and_post_order,
                order_args,
                None,
            )
            order_id = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else None
            if order_id:
                logger.info(f"Order placed OK | ID: {order_id}")
                return order_id
            logger.error(f"Order response missing ID: {resp}")
            return None
        except Exception as e:
            logger.error(f"Order placement failed: {e}", exc_info=True)
            return None

    async def place_maker_pair(self, market: Market) -> Tuple[Optional[str], Optional[str]]:
        """
        Place maker limit orders on BOTH sides of a market.
        Returns (yes_order_id, no_order_id).
        YES order: limit buy at (mid - MAKER_SPREAD_TARGET/2)
        NO order:  limit buy at (1-mid - MAKER_SPREAD_TARGET/2) on the NO side
        Both are maker orders (limit, not taker).
        """
        ob = market.order_book
        if not ob:
            return None, None
        mid = ob.mid_price
        if mid is None or mid <= 0 or mid >= 1:
            return None, None
        half = self.config.MAKER_SPREAD_TARGET / 2
        yes_price = round(max(0.01, mid - half), 4)
        no_price = round(max(0.01, (1 - mid) - half), 4)
        max_per_trade = getattr(self.config, "MAX_POSITION_SIZE_USD", self.config.MAX_BET_SIZE)
        size_usdc = min(
            self.config.MAKER_MAX_POSITION_SIZE,
            max_per_trade,
        )
        yes_shares = round(size_usdc / yes_price, 4)
        no_shares = round(size_usdc / no_price, 4)
        if self.config.PAPER_TRADING or getattr(self.config, "DRY_RUN", True):
            yes_id = f"paper-maker-yes-{int(time.time() * 1000)}"
            no_id = f"paper-maker-no-{int(time.time() * 1000)}"
            logger.info(
                f"PAPER: Maker pair YES @ {yes_price:.4f} NO @ {no_price:.4f} | "
                f"{market.question[:40]}"
            )
            return yes_id, no_id
        yes_id, no_id = None, None
        try:
            from py_clob_client.clob_types import OrderArgs

            py_client = self._get_py_clob()
            for token_id, price, shares, label in [
                (market.yes_token_id, yes_price, yes_shares, "YES"),
                (market.no_token_id, no_price, no_shares, "NO"),
            ]:
                order_args = OrderArgs(token_id=token_id, price=price, size=shares, side="BUY")
                resp = await asyncio.to_thread(py_client.create_and_post_order, order_args, None)
                oid = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else None
                if label == "YES":
                    yes_id = oid
                else:
                    no_id = oid
            logger.info(f"Maker pair placed | YES {yes_id} NO {no_id}")
        except Exception as e:
            logger.error(f"Maker pair failed: {e}", exc_info=True)
        return yes_id, no_id

    async def cancel_order(self, order_id: str) -> bool:
        if self.config.PAPER_TRADING:
            logger.info(f"PAPER TRADING: Simulated cancel for {order_id}")
            return True
        try:
            py_client = self._get_py_clob()
            await asyncio.to_thread(py_client.cancel, order_id)
            logger.info(f"Order {order_id} cancelled")
            return True
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    async def place_exit_order(
        self,
        token_id: str,
        shares: float,
        min_price: float,
        label: str = "EXIT"
    ) -> Optional[str]:
        """Place a sell order to exit an existing position."""
        if self.config.PAPER_TRADING:
            order_id = f"paper-exit-{int(time.time() * 1000)}"
            logger.info(f"PAPER TRADING: Simulated {label} order | ID: {order_id} | price>={min_price:.4f}")
            return order_id
        try:
            from py_clob_client.clob_types import OrderArgs

            py_client = self._get_py_clob()
            order_args = OrderArgs(
                token_id=token_id,
                price=round(min_price, 4),
                size=round(shares, 4),
                side="SELL",
            )
            resp = await asyncio.to_thread(py_client.create_and_post_order, order_args, None)
            order_id = resp.get("orderID") or resp.get("id") if isinstance(resp, dict) else None
            logger.info(f"{label} order placed | ID: {order_id} | price>={min_price:.4f}")
            return order_id
        except Exception as e:
            logger.error(f"{label} order failed: {e}", exc_info=True)
            return None

    def _compute_limit_price(self, market: Market, edge: EdgeResult) -> Optional[float]:
        """
        Compute a limit price that:
          1. Is aggressive enough to get filled quickly
          2. Stays within slippage tolerance
        """
        ob = market.order_book
        if not ob:
            return None

        if edge.side == Side.YES:
            best_ask = ob.best_yes_ask
            if best_ask is None:
                return None
            mid = ob.mid_price or best_ask
            # Pay up to slippage tolerance above mid
            limit = min(best_ask * 1.005, mid * (1 + self.config.SLIPPAGE_TOLERANCE))
        else:
            # Buying NO = selling YES side: look at bid side
            best_bid = ob.best_yes_bid
            if best_bid is None:
                return None
            no_ask = 1.0 - best_bid
            mid = 1.0 - (ob.mid_price or best_bid)
            limit = min(no_ask * 1.005, mid * (1 + self.config.SLIPPAGE_TOLERANCE))

        return round(min(limit, 0.99), 4)

    def _build_order_payload(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,  # "BUY" or "SELL"
    ) -> dict:
        """
        Build the order payload.

        Note: Production Polymarket orders require EIP-712 signing via
        the py-clob-client library. This payload structure follows the
        official Polymarket CLOB API spec. Install py-clob-client and
        use ClobClient.create_order() which handles signing automatically
        when initialized with a private key.

        See: https://github.com/Polymarket/py-clob-client
        """
        return {
            "tokenID": token_id,
            "price": str(price),
            "size": str(size),
            "side": side,
            "orderType": self.config.ORDER_TYPE,  # GTC or FOK
            "timeInForce": self.config.ORDER_TYPE,
            "nonce": str(int(time.time() * 1000)),
        }
