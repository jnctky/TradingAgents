"""Vendor-dispatched OHLCV loader.

``route_to_vendor`` returns agent-facing strings (including failure sentinels),
which is the wrong shape for the verification snapshot: it needs a DataFrame or
an exception. This module keeps the same "configured chain is the chain"
semantics for that one call, so the snapshot verifies prices with the same
vendor precedence the analyst's tools use.
"""

from __future__ import annotations

import logging

import pandas as pd

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import is_crypto_symbol

logger = logging.getLogger(__name__)

DEFAULT_CHAIN = ("sidecar", "openbb")

#: A coin has no equity bars; the equity chain can only reject the request.
#: Settlement and the verification snapshot read prices here, not through the
#: router, so this chain has to answer crypto on its own terms.
CRYPTO_CHAIN = ("binance", "sidecar")


def _loaders() -> dict[str, object]:
    """Import lazily so a missing optional vendor cannot break module import."""
    from .crypto_binance import load_ohlcv as binance_loader
    from .openbb.ohlcv import load_ohlcv as openbb_loader
    from .sidecar.market import load_ohlcv as sidecar_loader

    # yfinance is intentionally not registered: it is throttled on this network.
    # Binance is keyed separately and only reached for crypto symbols, so an
    # equity never pays a failed hop for it.
    return {"sidecar": sidecar_loader, "openbb": openbb_loader, "binance": binance_loader}


def configured_chain() -> list[str]:
    raw = (get_config().get("data_vendors") or {}).get("core_stock_apis") or ""
    chain = [v.strip() for v in str(raw).split(",") if v.strip() and v.strip() != "default"]
    return chain or list(DEFAULT_CHAIN)


def resolve_chain(symbol: str, loaders: dict[str, object]) -> list[str]:
    """The chain that can actually serve ``symbol``.

    Crypto takes Binance first — trying the equity chain only produced a 400
    from sidecar and a 204 from OpenBB before the same data was available. The
    configured vendors still follow it as fallbacks, and a config that names
    Binance explicitly keeps its own order.
    """
    chain = configured_chain()
    if is_crypto_symbol(symbol) and "binance" not in chain:
        chain = ["binance", *chain]
    return [v for v in chain if v in loaders]


def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True) -> pd.DataFrame:
    """OHLCV for ``symbol`` on or before ``as_of_date`` from the first vendor that answers."""
    loaders = _loaders()
    chain = resolve_chain(symbol, loaders)
    if not chain:
        raise VendorUnavailableError(
            f"no OHLCV-capable vendor in the configured chain "
            f"{configured_chain()}; available: {sorted(loaders)}"
        )

    last_no_data: NoMarketDataError | None = None
    last_error: Exception | None = None
    for vendor in chain:
        try:
            frame = loaders[vendor](symbol, as_of_date, fill_gaps=fill_gaps)
        except NoMarketDataError as exc:
            logger.warning("%s has no OHLCV for %s: %s; trying next vendor.", vendor, symbol, exc)
            last_no_data = exc
            continue
        except Exception as exc:  # noqa: BLE001 — a throttled vendor must not end the run
            logger.warning("%s failed loading OHLCV for %s: %s; trying next vendor.",
                           vendor, symbol, exc)
            last_error = exc
            continue
        if frame is not None and not frame.empty:
            return frame

    # A vendor that errored never said whether it has the symbol, so that
    # ambiguity outranks another vendor's clean "no data" — mirroring the router.
    if last_error is not None:
        raise VendorUnavailableError(
            f"no OHLCV vendor could serve {symbol} (last error: {last_error})"
        ) from last_error
    if last_no_data is not None:
        raise last_no_data
    raise NoMarketDataError(symbol, detail="no configured vendor returned price rows")
