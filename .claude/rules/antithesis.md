# Antithesis Rules

## SDK assertions
- `assert_raw()` is the only way to register dynamic-name assertions — f-strings are invisible to the static scanner.
- `TX_TYPES` derives from `REGISTRY` (single source of truth). Each entry gets `seen`/`success`/`failure` at startup via `register_assertions()`.
- `reachable` — reached ≥ once. `sometimes(success)` — succeeds ≥ once. `sometimes(failure)` — fails ≥ once. `always` — invariant. `unreachable` — must never fire (fatal errors).
- `_META_EXPECTATIONS` (`assertions.py`) — per-type `always` that a tesSUCCESS touched the expected `LedgerEntryType` under the allowed node op(s). Rows are verified against the rippled transactor (guaranteed on **every** success); create-or-update types list `Created`+`Modified`. Skips inner-batch txns (effects consolidate into the outer Batch's meta).
- `network_functional_after_faults` — `sometimes` liveness fed by the `/probe/network` endpoint (`probe.py`, driven by `eventually_network_probe.sh`): a payment must validate tesSUCCESS once faults stop.
- Ledger-state invariant probe (`invariants.py`, `/check/invariants`, driven by `anytime_check_ledger_invariants.sh`) — re-queries **xrpld's** validated ledger and fires structural `always`: `xrp_balance_non_negative` (must_hit=True — genesis accounts always queryable, doubles as "probe ran"), and offer-book invariants over the books `offer_create` populates (from `w.amms` non-MPT pairs): `offer_has_required_fields`, `offer_amounts_positive`, `offers_sorted_by_quality` (all must_hit=False — a book may stay empty a whole run) + `offer_book_has_offers` (`sometimes`, coverage). Every check skips on RPC failure/`actNotFound` (absence of evidence ≠ violation). Reads xrpld only — cross-validator agreement and XRP supply are the sidecar's job. Deliberately NOT ported: base-reserve floor and balance-within-limit (both legitimately violable → false positives).
- Sidecar (`sidecar/sidecar.py`) `always`: consensus safety (agree on hash per index), liveness (no unrecovered stall / close_time skew past `--max-stall`), and **XRP supply never increases** (`total_coins` monotone non-increasing as a validator's ledger advances — fees only burn).

## SDK handler chain (Python)
`VoidstarHandler` (libvoidstar.so, injected by Antithesis) → `LocalHandler` (`ANTITHESIS_SDK_LOCAL_OUTPUT`) → `NoopHandler` (silent). Outside Antithesis: `export ANTITHESIS_SDK_LOCAL_OUTPUT=/path/file.jsonl`.

## Test composer phases
```
setup_complete() -> [first_*] -> [drivers + anytime_*] -> [eventually_* / finally_*]
                    no faults     faults active           faults stopped
```
Implemented: `parallel_driver_*` (drivers) and `eventually_network_probe.sh` (post-fault liveness probe). Composer picks scripts by filename prefix, so an `eventually_*` in the driver folder runs only in the eventually phase.

## Network topology
Workload submits to the non-validating tracking node (`xrpld`), isolated from fault injection. The 6 validators (`val0`–`val4`, `fuzzer`) take faults. Don't retarget submission to a validator — fault jitter would corrupt transaction results.
</content>
