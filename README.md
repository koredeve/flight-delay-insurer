# FlightDelayInsurer

Parametric flight-delay insurance on GenLayer. A traveler buys a policy by attaching a premium; if live flight data later shows the flight was cancelled, diverted, or delayed past the policy's threshold (in minutes), the payout — `premium x 10` — is credited automatically to the insured and can be withdrawn at any time. No claims adjusters, no paperwork: settlement is driven entirely by an on-chain check against a flight-status API.

## Architecture

- **User action**: `buy_policy(policy_id, flight, date_iso, threshold_minutes)` with attached value (the premium). Payout is fixed at `premium x 10` (`PAYOUT_MULTIPLIER`).
- **Evidence source**: any public flight-status endpoint returning JSON like `{"status": "DELAYED", "delay_minutes": 180}`. The URL is supplied per-check via `check_status(policy_id, source_url)`.
- **Nondet call**: the leader runs `gl.nondet.web.get(source_url)`, maps HTTP 4xx to `[EXTERNAL]` and 5xx to `[TRANSIENT]`, parses the body defensively (tolerating missing/malformed `status` / `delay_minutes` fields), and computes `triggered = status.upper() in ("CANCELLED", "DIVERTED") or delay_minutes >= threshold`.
- **Equivalence principle**: custom validator reruns the leader fetch independently and agrees only if the **triggered boolean matches exactly AND the reported delays differ by at most ±10 minutes** (jitter tolerance for fast-moving feeds). Error paths follow the canonical `_handle_leader_error` rules.
- **Settlement effect**: on trigger, the policy flips to `paid`, the observed delay is persisted, and `payout_atto` is added to the insured's credit balance (withdrawable via `withdraw()`); otherwise it flips to `denied`. Either way the check is final — re-checking a resolved policy reverts.
- **Appeal path**: GenLayer Optimistic Democracy gives leader-proposes / validator-check / appeal window natively; no extra contract machinery is required.

## Quickstart

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# lint
genvm-lint check contracts/FlightDelayInsurer.py --json

# tests
pytest tests/direct/ -v
```

## Interface

| Method | Type | Notes |
| --- | --- | --- |
| `owner()` | view | Deployer address (stored as `owner_addr`). |
| `buy_policy(policy_id, flight, date_iso, threshold_minutes)` | write, payable | Requires value > 0 and threshold > 0; ids unique; payout = premium × 10. |
| `check_status(policy_id, source_url)` | write | Active policies only; fetches live data, settles paid/denied, persists delay. |
| `withdraw()` | write | Transfers accumulated credits to caller; reverts with nothing owed. |
| `get_policy(policy_id)` | view | Full policy record; `insured` exposed as string. |
| `credit_of(who)` | view | Withdrawable balance for an address. |
| `total_policies()` | view | Number of policies sold. |

## StudioNet

StudioNet is gasless — deploying and interacting costs 0 GEN, so all flows above can be exercised freely in the studio environment.
