"""
auth.py — Derive CLOB API credentials from the private key.

Polymarket has two credential systems:
- Builder Keys (polymarket.com/settings?tab=builder): Order attribution only, NOT for CLOB auth.
- CLOB API credentials: Derived from POLY_PRIVATE_KEY via create_or_derive_api_creds().
  These authenticate balance checks, order placement, cancellations, etc.

When POLY_API_KEY is empty, the bot derives CLOB creds from POLY_PRIVATE_KEY at startup.
"""

import logging
from typing import Optional

from config import BotConfig

logger = logging.getLogger("auth")


def derive_clob_creds(config: BotConfig) -> Optional[dict]:
    """
    Derive CLOB API credentials from POLY_PRIVATE_KEY using py-clob-client.
    Returns dict with api_key, api_secret, api_passphrase, or None on failure.
    Uses same signature_type/funder as the executor (proxy vs EOA).
    """
    if not (config.PRIVATE_KEY or "").strip():
        return None
    try:
        from py_clob_client.client import ClobClient as PyClobClient

        host = (config.CLOB_API_URL or "https://clob.polymarket.com").rstrip("/")
        sig_type = 1 if getattr(config, "PROXY_WALLET", None) else 0
        funder = getattr(config, "PROXY_WALLET", None) or None

        client = PyClobClient(
            host=host,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
            creds=None,  # L1 auth only for derive
            signature_type=sig_type,
            funder=funder,
        )
        creds = client.create_or_derive_api_creds()
        if not creds:
            return None
        if isinstance(creds, dict):
            return creds
        return {
            "api_key": getattr(creds, "api_key", ""),
            "api_secret": getattr(creds, "api_secret", ""),
            "api_passphrase": getattr(creds, "api_passphrase", ""),
        }
    except Exception as e:
        logger.error(f"Failed to derive CLOB credentials: {e}", exc_info=True)
        return None


def get_signer_address(config: BotConfig) -> Optional[str]:
    """
    Derive Polygon signer (EOA) address from PRIVATE_KEY. Required for L2 POLY-ADDRESS header.
    Polymarket docs require POLY_ADDRESS on all authenticated endpoints.
    """
    key = (config.PRIVATE_KEY or "").strip()
    if not key:
        return getattr(config, "PROXY_WALLET", None)
    try:
        from py_clob_client.client import ClobClient as PyClobClient
        client = PyClobClient(
            host=(config.CLOB_API_URL or "https://clob.polymarket.com").rstrip("/"),
            chain_id=config.CHAIN_ID,
            key=key,
            creds=None,
        )
        addr = client.get_address()
        if addr:
            return addr if addr.startswith("0x") else f"0x{addr}"
    except Exception as e:
        logger.warning(f"Could not derive signer address: {e}")
    return getattr(config, "PROXY_WALLET", None)


def ensure_clob_creds(config: BotConfig, force_derive: bool = False) -> bool:
    """
    Ensure config has valid CLOB API credentials. Derives from POLY_PRIVATE_KEY when:
    - force_derive=True (POLY_DERIVE_CREDS env), or
    - API_KEY is empty and PRIVATE_KEY is set.
    Builder Keys (polymarket.com/settings?tab=builder) are for order attribution only,
    NOT CLOB auth — they cause 401. Use derived creds instead.
    """
    if not force_derive and config.API_KEY and config.API_SECRET:
        # Ensure POLY-ADDRESS for L2; derive from PRIVATE_KEY if not set
        if not getattr(config, "SIGNER_ADDRESS", None) and config.PRIVATE_KEY:
            signer_addr = get_signer_address(config)
            if signer_addr:
                config.SIGNER_ADDRESS = signer_addr  # type: ignore
        return True
    if not config.PRIVATE_KEY:
        return bool(config.API_KEY)
    derived = derive_clob_creds(config)
    if derived:
        config.API_KEY = str(derived.get("api_key", derived.get("apiKey", "")))
        config.API_SECRET = str(derived.get("api_secret", derived.get("secret", "")))
        config.API_PASSPHRASE = str(derived.get("api_passphrase", derived.get("passphrase", "")))
        # POLY-ADDRESS required for L2; use signer (EOA) or PROXY_WALLET fallback
        signer_addr = get_signer_address(config)
        if signer_addr:
            config.SIGNER_ADDRESS = signer_addr  # type: ignore
        logger.info("Derived CLOB API credentials from POLY_PRIVATE_KEY")
        return True
    return False
