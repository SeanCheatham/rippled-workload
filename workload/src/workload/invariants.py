"""Ledger-state invariant probe.

Re-queries xrpld's validated ledger and asserts structural/accounting invariants
the per-tx assertions can't see — the class of check ported (selectively) from the
antiralph-ripple harness. Reads xrpld only: cross-validator agreement and XRP supply
are the sidecar's job; this verifies the committed ledger is internally consistent.

Every assertion is guarded so transient fault-window RPC failures never fire
falsely (absence of evidence is not evidence of a violation). Deliberately omitted
from the antiralph set: the base-reserve floor (antiralph itself demoted it to
`sometimes` — fee claims legitimately push balances below reserve) and
balance-within-limit (a lowered TrustSet limit can legitimately sit below an
already-accrued balance). Both are false positives, not invariants.

Driven by ``anytime_check_ledger_invariants.sh`` (faults-active phase).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from xrpl.models import XRP
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.requests import AccountInfo, BookOffers, ServerInfo

from workload.assertions import (
    INV_OFFER_BOOK_HAS_OFFERS,
    INV_OFFER_FIELDS,
    INV_OFFER_POSITIVE,
    INV_OFFER_SORTED,
    INV_XRP_NON_NEGATIVE,
    assert_invariant,
)
from workload.randoms import sample

if TYPE_CHECKING:
    from xrpl.models.currencies import Currency

    from workload.app import Workload
    from workload.models import AMM

# Bound RPC load per invocation (runs alongside fault injection); Antithesis varies
# coverage across invocations by resampling.
_MAX_ACCOUNTS = 25
_MAX_BOOKS = 6
_BOOK_LIMIT = 50


def _amount_value(amount: object) -> float | None:
    """Numeric value of a book_offers TakerGets/TakerPays (XRP drops string or IOU dict)."""
    if isinstance(amount, str):
        try:
            return float(amount)
        except ValueError:
            return None
    if isinstance(amount, dict):
        val = amount.get("value")
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return None


async def _check_xrp_balances(w: Workload) -> None:
    addrs = list(w.accounts.keys())
    if not addrs:
        return
    for addr in sample(addrs, min(_MAX_ACCOUNTS, len(addrs))):
        try:
            resp = await w.client.request(AccountInfo(account=addr, ledger_index="validated"))
        except Exception:
            continue  # transient fault-window failure
        if not resp.is_successful():
            continue  # actNotFound (deleted/unfunded) or noNetwork — skip, don't fire
        balance = resp.result.get("account_data", {}).get("Balance")
        try:
            drops = int(balance)
        except (TypeError, ValueError):
            continue
        # Balance is an unsigned drops count — negativity is physically impossible
        # in a well-formed ledger, so a fire is an unambiguous accounting bug.
        assert_invariant(
            drops >= 0, INV_XRP_NON_NEGATIVE, {"account": addr, "balance_drops": drops}
        )


def _book_currency(asset: Currency) -> Currency | None:
    """The book side for an AMM asset; None for MPT (covered by mpt_dex, not here)."""
    if isinstance(asset, (IssuedCurrency, XRP)):
        return asset
    return None


def _currency_key(cur: Currency) -> tuple[str, str]:
    if isinstance(cur, IssuedCurrency):
        return (cur.currency, cur.issuer)
    return ("XRP", "")


def _candidate_books(amms: list[AMM]) -> list[tuple[Currency, Currency]]:
    """Directed (taker_gets, taker_pays) books from non-MPT AMM pairs — exactly the
    books ``offer_create`` rests offers in. Deduped across AMMs."""
    seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    books: list[tuple[Currency, Currency]] = []
    for amm in amms:
        if len(amm.assets) < 2:
            continue
        c0 = _book_currency(amm.assets[0])
        c1 = _book_currency(amm.assets[1])
        if c0 is None or c1 is None:
            continue
        k0, k1 = _currency_key(c0), _currency_key(c1)
        for gets, pays, kg, kp in ((c0, c1, k0, k1), (c1, c0, k1, k0)):
            if (kg, kp) in seen:
                continue
            seen.add((kg, kp))
            books.append((gets, pays))
    return books


def _check_offers_structural(offers: list[dict]) -> None:
    """Resting-book invariants: fields present, amounts positive, ascending quality.
    The ordering chain resets on any offer with unparseable quality to avoid comparing
    across a gap (a false-positive source in the source harness)."""
    prev_quality: float | None = None
    for idx, offer in enumerate(offers):
        taker_pays = offer.get("TakerPays")
        taker_gets = offer.get("TakerGets")
        has_fields = taker_pays is not None and taker_gets is not None
        assert_invariant(
            has_fields,
            INV_OFFER_FIELDS,
            {"offer_index": idx, "account": offer.get("Account", "")},
            must_hit=False,
        )
        if not has_fields:
            prev_quality = None
            continue

        pays_v = _amount_value(taker_pays)
        gets_v = _amount_value(taker_gets)
        positive = pays_v is not None and pays_v > 0 and gets_v is not None and gets_v > 0
        assert_invariant(
            positive,
            INV_OFFER_POSITIVE,
            {
                "offer_index": idx,
                "taker_pays": str(taker_pays),
                "taker_gets": str(taker_gets),
            },
            must_hit=False,
        )

        # Compare only rippled's own quality field — never a pays/gets ratio, which
        # ignores transfer rates and would diverge from rippled's real sort key (a
        # false-positive vector). Absent/unparseable quality resets the chain.
        quality: float | None = None
        q = offer.get("quality")
        if q is not None:
            try:
                quality = float(q)
            except (TypeError, ValueError):
                quality = None

        if quality is not None and prev_quality is not None:
            assert_invariant(
                prev_quality <= quality,
                INV_OFFER_SORTED,
                {"offer_index": idx, "prev_quality": prev_quality, "quality": quality},
                must_hit=False,
            )
        prev_quality = quality  # None resets the chain


async def _check_offer_books(w: Workload) -> None:
    books = _candidate_books(w.amms)
    if not books:
        return
    for gets, pays in sample(books, min(_MAX_BOOKS, len(books))):
        try:
            resp = await w.client.request(
                BookOffers(
                    taker_gets=gets,
                    taker_pays=pays,
                    ledger_index="validated",
                    limit=_BOOK_LIMIT,
                )
            )
        except Exception:
            continue
        if not resp.is_successful():
            continue
        offers = resp.result.get("offers", [])
        # Coverage only (must_hit=False): offers may all cross/IOC, so a book can
        # legitimately stay empty for a whole run — requiring a hit would starve.
        assert_invariant(
            len(offers) > 0,
            INV_OFFER_BOOK_HAS_OFFERS,
            {"offer_count": len(offers)},
            assert_type="sometimes",
            display_type="Sometimes",
            must_hit=False,
        )
        _check_offers_structural(offers)


async def check_ledger_invariants(w: Workload) -> bool:
    """Query xrpld's validated ledger and fire the structural invariants. Never raises;
    returns False when no validated ledger is reachable (probe skipped this cycle)."""
    try:
        info = await w.client.request(ServerInfo())
        validated = (
            info.result.get("info", {}).get("validated_ledger") if info.is_successful() else None
        )
    except Exception:
        return False
    if not validated:
        return False
    await _check_xrp_balances(w)
    await _check_offer_books(w)
    return True
