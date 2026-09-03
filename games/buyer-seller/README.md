# Buyer-Seller

Sequential multi-item bargaining under asymmetric information. A seller with `k`
identical units and private cost `c` makes alternating offers to a buyer with
private valuation `v`, under a discount factor `δ` and a deadline `T`.

Full protocol: [`SPEC.md`](SPEC.md).

## What it measures

Bargaining is where communication has a *price*. Every round of haggling costs
both sides `δ` of their surplus, so the environment separates agents that extract value
from agents that destroy it by talking too long. It also has a clean
efficiency benchmark — the first-best is computable in closed form — which most
open-ended communication games lack.

Because `c` and `v` are private, the only channel for learning is the sequence of
accepted and rejected offers. That makes it a direct test of inference from
behavior rather than from stated intent.

## Quick start

```bash
uv pip install -e games/buyer-seller

# no API keys: scripted agents, episodes into SQLite
a2a-run games/buyer-seller/experiments/example.yaml --smoke-test

# the real thing
cd games/buyer-seller && uv run python run.py experiments/example.yaml
```

## Configuration

| field | symbol | meaning |
|---|---|---|
| `num_items` | k | units for sale, at most one per round |
| `seller_cost` | c | seller's reservation cost — **private** |
| `buyer_value` | v | buyer's per-unit valuation — **private** |
| `max_rounds` | T | deadline; no agreement by T means zero for both |
| `discount_factor` | δ | payoffs multiply by `δ^(t-1)` |
| `enforce_monotonic_offers` | — | SPEC §5: require `p_t ≤ p_{t-1}` |
| `opening_price` | — | seller's first ask (default: `2.5 × c`) |
| `buyer_patience` | — | scripted-agent knob: hold out until `p ≤ v × patience` |

## Metrics

| metric | meaning |
|---|---|
| `units_sold`, `sell_through` | how much of the inventory cleared |
| `buyer_utility`, `seller_utility`, `joint_utility` | discounted surplus per SPEC §4 |
| `efficiency` | joint utility over the first-best achievable under this protocol |
| `buyer_share` | split of the realised surplus — who captured the gains |
| `rounds_used`, `price_concession` | how long it took and how far the seller moved |
| `mean_strike_price`, `first_offer`, `final_offer` | price path summary |
| `monotonic_violations` | offers clamped by the §5 constraint |

`efficiency` divides by `Σ δ^i (v − c)` over the `k` units — the best attainable
when every unit trades as early as the protocol allows, one per round. Dividing
by the undiscounted `k(v − c)` would make 1.0 unreachable by construction and
quietly understate every agent.

## Two interpretation choices

`SPEC.md` is silent on both; the environment states them and the tests pin them.

**The clock advances on accepts.** A round is one offer/response exchange, and
`t` increments whether or not the unit sold. Otherwise several units could settle
at the same `t` and `δ^(t-1)` would stop distinguishing fast agreement from slow.

**Monotonicity violations are clamped, not rejected.** With
`enforce_monotonic_offers`, a seller that raises its price gets the offer clamped
to the previous price and a `monotonic_violation` flag on the event. Erroring out
would discard an otherwise complete trajectory over what is usually a formatting
error, and the flag keeps the violation measurable.

## Suggested conditions

The shipped `experiments/example.yaml` varies surplus width and includes a
`no_gains_control` cell where `v < c`. That control is the one worth keeping:
any units sold there are loss-making trades, which is a clean measure of whether
an agent is actually reasoning about its reservation value or just being
agreeable.
