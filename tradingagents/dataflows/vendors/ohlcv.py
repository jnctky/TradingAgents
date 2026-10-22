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

logger = logging.getLogger(__name__)

DEFAULT_CHAIN = ("openbb", "yfinance")


def _loaders() -> dict[str, object]:
    """Import lazily so a missing optional vendor cannot break module import."""
    from .openbb.ohlcv import load_ohlcv as openbb_loader
    from .yahoo.ohlcv import load_ohlcv as yahoo_loader

    return {"openbb": openbb_loader, "yfinance": yahoo_loader}


def configured_chain() -> list[str]:
    raw = (get_config().get("data_vendors") or {}).get("core_stock_apis") or ""
    chain = [v.strip() for v in str(raw).split(",") if v.strip() and v.strip() != "default"]
    return chain or list(DEFAULT_CHAIN)


def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True) -> pd.DataFrame:
    """OHLCV for ``symbol`` on or before ``as_of_date`` from the first vendor that answers."""
    loaders = _loaders()
    chain = [v for v in configured_chain() if v in loaders]
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
