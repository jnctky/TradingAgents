from __future__ import annotations

import logging
from typing import Any

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.trace import record_call, stopwatch
from tradingagents.dataflows.symbols import is_crypto_symbol
from tradingagents.dataflows.vendors.alpha_vantage import (
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_global_news as get_alpha_vantage_global_news,
    get_income_statement as get_alpha_vantage_income_statement,
    get_indicator as get_alpha_vantage_indicator,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_stock as get_alpha_vantage_stock,
)
from tradingagents.dataflows.vendors.crypto_binance import (
    get_stock_data as get_binance_stock,
)
from tradingagents.dataflows.vendors.crypto_binance import (
    get_stock_stats_indicators_window as get_binance_indicator,
)
from tradingagents.dataflows.vendors.fred import get_macro_data as get_fred_macro_data
from tradingagents.dataflows.vendors.openbb import (
    get_balance_sheet as get_openbb_balance_sheet,
    get_cashflow as get_openbb_cashflow,
    get_fundamentals as get_openbb_fundamentals,
    get_global_news_openbb as get_openbb_global_news,
    get_income_statement as get_openbb_income_statement,
    get_insider_transactions as get_openbb_insider_transactions,
    get_macro_data as get_openbb_macro,
    get_news_openbb as get_openbb_news,
    get_stock_data as get_openbb_stock,
    get_stock_stats_indicators_window as get_openbb_indicator,
)
from tradingagents.dataflows.vendors.polymarket import (
    get_prediction_markets as get_polymarket_prediction_markets,
)
from tradingagents.dataflows.vendors.sec_edgar import (
    get_balance_sheet as get_sec_edgar_balance_sheet,
    get_cashflow as get_sec_edgar_cashflow,
    get_income_statement as get_sec_edgar_income_statement,
)
from tradingagents.dataflows.vendors.sidecar import (
    get_balance_sheet as get_sidecar_balance_sheet,
    get_cashflow as get_sidecar_cashflow,
    get_fundamentals as get_sidecar_fundamentals,
    get_income_statement as get_sidecar_income_statement,
    get_news as get_sidecar_news,
    get_stock_data as get_sidecar_stock,
    get_stock_stats_indicators_window as get_sidecar_indicator,
)

logger = logging.getLogger(__name__)

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ]
    },
    "macro_data": {
        "description": "Macroeconomic indicators (rates, inflation, labor, growth)",
        "tools": [
            "get_macro_indicators",
        ]
    },
    "prediction_markets": {
        "description": "Market-implied probabilities for forward-looking events",
        "tools": [
            "get_prediction_markets",
        ]
    }
}

# Optional enrichment categories. These add macro/event context to the news
# analyst but are not core to a decision, so a vendor failure here degrades to a
# sentinel instead of aborting the run (a bad LLM-supplied indicator, a missing
# key, or a network blip should not crash an analysis over flavour data). Core
# categories (prices, fundamentals, news) still raise so a broken primary is loud.
OPTIONAL_CATEGORIES = {"macro_data", "prediction_markets"}

#: Methods whose answer is a price series. These are the only calls where the
#: right vendor depends on which market the instrument trades in: an equity
#: vendor answers a coin with a rejection (sidecar 400, OpenBB 204), which the
#: router would otherwise record as "no data for the instrument".
PRICE_METHODS = frozenset({"get_stock_data", "get_indicators"})

#: Vendor chain for crypto price series (user decision, 2026-11-11): Binance
#: spot first, the akshare sidecar as fallback for the coins it can still reach
#: (BTC via CME futures). Equity vendors are dropped rather than tried-and-
#: failed — they can never serve a coin, and a doomed hop only adds latency and
#: a misleading "vendor failed" line to the trace.
CRYPTO_VENDOR_CHAIN = ("binance", "sidecar")

#: Fundamental statements exist for issuers, not for coins. Answering with a
#: sentinel keeps "crypto has no fundamentals" from being reported as a fetch
#: failure — the integrity summary labels it 不适用 instead of 失败.
CRYPTO_FUNDAMENTAL_METHODS = frozenset({
    "get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement",
})

CRYPTO_FUNDAMENTAL_SENTINEL = (
    "NOT_APPLICABLE: {method} for '{symbol}' — crypto assets have no financial"
    " statements. This is not a data outage; do not substitute on-chain or"
    " exchange metrics for issuer fundamentals, and do not treat the absence as"
    " a data-quality problem."
)

# Mapping of methods to their vendor-specific implementations
VENDOR_METHODS = {
    # core_stock_apis
    "get_stock_data": {
        "binance": get_binance_stock,
        "sidecar": get_sidecar_stock,
        "openbb": get_openbb_stock,
        "alpha_vantage": get_alpha_vantage_stock,
    },
    # technical_indicators
    "get_indicators": {
        "binance": get_binance_indicator,
        "sidecar": get_sidecar_indicator,
        "openbb": get_openbb_indicator,
        "alpha_vantage": get_alpha_vantage_indicator,
    },
    # fundamental_data
    "get_fundamentals": {
        "sidecar": get_sidecar_fundamentals,
        "openbb": get_openbb_fundamentals,
        "alpha_vantage": get_alpha_vantage_fundamentals,
    },
    "get_balance_sheet": {
        "sec_edgar": get_sec_edgar_balance_sheet,
        "sidecar": get_sidecar_balance_sheet,
        "openbb": get_openbb_balance_sheet,
        "alpha_vantage": get_alpha_vantage_balance_sheet,
    },
    "get_cashflow": {
        "sec_edgar": get_sec_edgar_cashflow,
        "sidecar": get_sidecar_cashflow,
        "openbb": get_openbb_cashflow,
        "alpha_vantage": get_alpha_vantage_cashflow,
    },
    "get_income_statement": {
        "sec_edgar": get_sec_edgar_income_statement,
        "sidecar": get_sidecar_income_statement,
        "openbb": get_openbb_income_statement,
        "alpha_vantage": get_alpha_vantage_income_statement,
    },
    # news_data
    "get_news": {
        "sidecar": get_sidecar_news,
        "openbb": get_openbb_news,
        "alpha_vantage": get_alpha_vantage_news,
    },
    "get_global_news": {
        "openbb": get_openbb_global_news,
        "alpha_vantage": get_alpha_vantage_global_news,
    },
    "get_insider_transactions": {
        "openbb": get_openbb_insider_transactions,
        "alpha_vantage": get_alpha_vantage_insider_transactions,
    },
    # macro_data
    "get_macro_indicators": {
        "openbb": get_openbb_macro,
        "fred": get_fred_macro_data,
    },
    # prediction_markets
    "get_prediction_markets": {
        "polymarket": get_polymarket_prediction_markets,
    },
}


def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")


def get_vendor(category: str, method: str = None) -> str:
    """Get the configured vendor for a data category or specific tool method.
    Tool-level configuration takes precedence over category-level.
    """
    config = get_config()

    # Check tool-level configuration first (if method provided)
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # Fall back to category-level configuration
    return config.get("data_vendors", {}).get(category, "default")


def vendor_unavailable(method: str, error: Exception) -> str:
    """What a call returns when every vendor was throttled or unreachable."""
    return (
        f"DATA_UNAVAILABLE: no configured vendor could serve {method} right now "
        f"({error}). This says nothing about the instrument; report the "
        f"data as unavailable and do not estimate or fabricate values."
    )


def no_data_available(error: NoMarketDataError) -> str:
    """What a call returns when every vendor that answered had no usable data."""
    resolved = "" if error.canonical == error.symbol else f" (resolved to '{error.canonical}')"
    # Surface the typed error's detail (e.g. "latest row is 2025-06-11 ...
    # stale") so the agent sees the specific reason — invalid symbol, no
    # coverage, or stale data — not just a generic "unavailable".
    reason = f" ({error.detail})" if error.detail else ""
    return (
        f"NO_DATA_AVAILABLE: No usable market data for '{error.symbol}'{resolved} from "
        f"any configured vendor{reason}. The symbol may be invalid, delisted, "
        f"not covered, or the vendor returned stale data. Do not estimate or "
        f"fabricate values — report that data is unavailable for this symbol."
    )


def _symbol_argument(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """The ticker a call is about — first positional string, else ``symbol=``."""
    if args and isinstance(args[0], str):
        return args[0]
    symbol = kwargs.get("symbol")
    return symbol if isinstance(symbol, str) else None


def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to appropriate vendor implementation with fallback support."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(',')]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    all_available_vendors = list(VENDOR_METHODS[method].keys())

    # Crypto splits off before the equity chain is considered: a coin has no
    # issuer statements and no equity bars, so the equity vendors can only
    # answer with a rejection that reads downstream as "no data for this
    # symbol". Sending the call to the crypto chain is what keeps that
    # ambiguity out of the report.
    symbol = _symbol_argument(args, kwargs)
    is_crypto = symbol is not None and is_crypto_symbol(symbol)

    if is_crypto and method in CRYPTO_FUNDAMENTAL_METHODS:
        sentinel = CRYPTO_FUNDAMENTAL_SENTINEL.format(method=method, symbol=symbol)
        record_call(method=method, category=category, vendor="not_applicable",
                    chain=["not_applicable"], args=args, kwargs=kwargs, ms=0,
                    result=sentinel)
        return sentinel

    # The configured vendor list IS the chain: we do NOT silently fall back to
    # vendors the user did not choose (#988/#289) — that returned data from an
    # unexpected source and caused cross-vendor inconsistencies. For multi-vendor
    # fallback, list them in order, e.g. data_vendors="yfinance,alpha_vantage".
    # The "default" sentinel (no explicit config) uses all available vendors.
    explicit = [v for v in primary_vendors if v and v != "default"]
    if explicit:
        vendor_chain = [v for v in explicit if v in VENDOR_METHODS[method]]
        if not vendor_chain:
            raise ValueError(
                f"Configured vendor(s) {explicit} not available for '{method}'. "
                f"Available: {all_available_vendors}."
            )
    else:
        vendor_chain = all_available_vendors

    # Crypto price series: the equity chain cannot serve a coin, so it is
    # replaced rather than tried. An explicit `binance` in the user's config
    # (tool-level or category-level) still wins — the user's chain is the
    # contract; this only fills in the case they never mentioned crypto.
    if is_crypto and method in PRICE_METHODS and "binance" not in primary_vendors:
        vendor_chain = [v for v in CRYPTO_VENDOR_CHAIN if v in VENDOR_METHODS[method]]

    last_no_data: NoMarketDataError | None = None
    last_unavailable: VendorUnavailableError | None = None
    failed: Exception | None = None     # a vendor that raised something untyped
    first_error: Exception | None = None
    chain_watch = stopwatch()
    chain_watch.__enter__()
    for vendor in vendor_chain:
        vendor_impl = VENDOR_METHODS[method][vendor]
        impl_func = vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

        try:
            with stopwatch() as watch:
                result = impl_func(*args, **kwargs)
            # Provenance is recorded here because this is the only frame that
            # knows which vendor actually answered — the payload text does not
            # say so consistently (indicators carry no source line at all).
            record_call(method=method, category=category, vendor=vendor,
                        chain=vendor_chain, args=args, kwargs=kwargs,
                        ms=watch.ms, result=result)
            return result
        except VendorUnavailableError as e:
            logger.warning("Vendor %r unavailable for %s: %s; trying next vendor.", vendor, method, e)
            # Kept so an all-unavailable chain can say the vendor was the
            # problem, rather than reporting nothing about the symbol.
            last_unavailable = e
            continue
        except VendorNotConfiguredError as e:
            logger.warning("Vendor %r not configured for %s; trying next vendor.", vendor, method)
            if first_error is None:
                first_error = e  # Surface it if no other vendor can serve the call.
            continue
        except NoMarketDataError as e:
            last_no_data = e  # No data here; another configured vendor may have it
            continue
        except Exception as e:
            # Don't let one vendor's failure crash the call when another can
            # serve it, but never swallow silently: a broken primary must be
            # visible in the logs (#989), not hidden behind a fallback's verdict.
            logger.warning("Vendor %r failed for %s: %s", vendor, method, e)
            if first_error is None:
                first_error = e
            failed = e
            continue

    # No vendor served this call. Keep one record of the miss so the cockpit can
    # distinguish "source returned nothing" from "nobody was ever asked" —
    # otherwise a silent gap looks like the analyst simply chose not to look.
    chain_watch.__exit__(None, None, None)
    record_call(method=method, category=category, vendor=None, chain=vendor_chain,
                args=args, kwargs=kwargs, ms=chain_watch.ms,
                error=str(first_error or last_no_data or last_unavailable or "no data"))

    # A vendor that throttled or failed the request never said whether it has
    # the symbol, so no other vendor's "no data" can speak for the whole chain:
    # report the vendors as the problem, not the instrument. It must not end
    # the run either.
    if last_unavailable is not None:
        return vendor_unavailable(method, last_unavailable)
    if failed is not None and last_no_data is not None:
        return vendor_unavailable(method, failed)

    # Every vendor that answered reported "no data": the symbol is genuinely unavailable.
    # Return one explicit, instructive sentinel rather than a vendor-specific
    # empty string, so the agent reports "unavailable" instead of inventing a
    # value. This takes precedence over incidental fallback errors.
    if last_no_data is not None:
        if first_error is not None:
            # A vendor also hit a real error; surface it in logs so the no-data
            # verdict can't hide a broken primary (network/auth/etc.).
            logger.warning(
                "Returning NO_DATA for %s, but a vendor errored earlier: %s",
                method, first_error,
            )
        return no_data_available(last_no_data)

    # No vendor returned data and none reported clean "no data" — surface the
    # first real error (e.g. the primary vendor's network failure). Optional
    # enrichment categories degrade to a sentinel instead, so flavour data can't
    # abort the run.
    if first_error is not None:
        if category in OPTIONAL_CATEGORIES:
            logger.warning("Optional %s unavailable for %s: %s", category, method, first_error)
            return (
                f"DATA_UNAVAILABLE: optional {category} could not be retrieved "
                f"({first_error}). Proceed without it; do not fabricate values."
            )
        raise first_error

    raise RuntimeError(f"No available vendor for '{method}'")
