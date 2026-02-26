"""
orphan_handler.py — Detects and reconciles CLOB positions not tracked by the bot.

Orphans can arise from:
  - Maker orders where only one leg filled (YES or NO)
  - Manual positions opened outside the bot
  - Bot restart with positions from a previous session

When orphans are found, they are added to the position manager for TP/SL/time-stop monitoring.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from position_manager import PositionManager

from config import BotConfig
from clob_client import ClobClient
from models import Market, Position, Side

# PositionManager import deferred to avoid circular import

logger = logging.getLogger("orphan_handler")


def _parse_clob_position(raw: dict) -> Optional[tuple]:
    """
    Parse CLOB position dict into (token_id, size, condition_id?, avg_price?).
    Polymarket may return: asset_id/token_id, size, condition_id, avgPrice, etc.
    """
    token_id = raw.get("asset_id") or raw.get("token_id") or raw.get("assetID") or raw.get("asset")
    size = raw.get("size")
    if token_id is None or size is None:
        return None
    try:
        size = float(size)
    except (TypeError, ValueError):
        return None
    if size < 0.01:
        return None  # Ignore dust
    condition_id = raw.get("condition_id") or raw.get("conditionId")
    avg_price = raw.get("avgPrice") or raw.get("avg_price")
    if avg_price is not None:
        try:
            avg_price = float(avg_price)
        except (TypeError, ValueError):
            avg_price = None
    return (str(token_id), size, condition_id, avg_price)


def _token_to_side(token_id: str, market: Market) -> Side:
    """Determine Side from token_id."""
    if token_id == market.yes_token_id:
        return Side.YES
    if token_id == market.no_token_id:
        return Side.NO
    raise ValueError(f"token_id {token_id} not in market")


class OrphanHandler:
    def __init__(
        self,
        config: BotConfig,
        client: ClobClient,
        position_manager: "PositionManager",
    ):
        self.config = config
        self.client = client
        self.position_manager = position_manager

    def _tracked_token_ids(self) -> set:
        """Token IDs we already track in position_manager."""
        return {
            p.token_id
            for p in self.position_manager.positions.values()
            if p.is_open
        }

    async def reconcile(
        self,
        markets: List[Market],
        add_orphans: bool = True,
    ) -> List[dict]:
        """
        Fetch CLOB positions, compare to tracked positions.
        Returns list of orphan info dicts.
        If add_orphans=True, adds orphans to position_manager for monitoring.
        """
        try:
            result = await self.client.get_positions()
        except Exception as e:
            logger.warning(f"Orphan reconciliation failed — could not fetch positions: {e}")
            return []

        positions_data = result if isinstance(result, list) else result.get("data", [])
        if not isinstance(positions_data, list):
            return []

        market_by_token: Dict[str, Market] = {}
        for m in markets:
            market_by_token[m.yes_token_id] = m
            market_by_token[m.no_token_id] = m

        tracked = self._tracked_token_ids()
        orphans: List[dict] = []

        for raw in positions_data:
            parsed = _parse_clob_position(raw)
            if not parsed:
                continue
            token_id, size, _cond_id, avg_price = parsed
            if token_id in tracked:
                continue
            market = market_by_token.get(token_id)
            if not market:
                continue
            try:
                side = _token_to_side(token_id, market)
            except ValueError:
                continue
            entry_price = avg_price if avg_price and avg_price > 0 else (0.5 if side == Side.YES else 0.5)
            size_usdc = size * entry_price if entry_price > 0 else 0
            # Prefer API's conditionId when present (exact match for this position)
            condition_id = _cond_id or market.condition_id
            orphans.append({
                "token_id": token_id,
                "condition_id": condition_id,
                "question": market.question,
                "side": side,
                "shares": size,
                "entry_price": entry_price,
                "size_usdc": size_usdc,
            })

        if orphans and add_orphans:
            for o in orphans:
                if self.position_manager.has_position(o["condition_id"]):
                    continue
                pos = Position(
                    condition_id=o["condition_id"],
                    question=o["question"],
                    side=o["side"],
                    token_id=o["token_id"],
                    entry_price=o["entry_price"],
                    size_usdc=o["size_usdc"],
                    shares=o["shares"],
                    entry_time=datetime.now(timezone.utc),
                    end_timestamp=0,
                    strategy_name="ORPHAN",
                )
                m = next((x for x in markets if x.condition_id == o["condition_id"]), None)
                if m:
                    pos.end_timestamp = m.end_timestamp
                self.position_manager.positions[o["condition_id"]] = pos
                logger.warning(
                    f"ORPHAN RECOVERED: {o['side'].value} {o['shares']:.2f} shares "
                    f"@ {o['entry_price']:.3f} | {o['question'][:50]}"
                )

        return orphans
