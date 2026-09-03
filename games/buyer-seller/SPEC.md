This protocol specification defines a sequential, multi-item bargaining environment between two agents under asymmetric information. It uses an alternating-offers format with a fixed depreciation penalty to incentivize truth-telling and prevent infinite bargaining gridlock.
------------------------------
## 1. Protocol Architecture & Structural Constants

* Seller ($S$): Holds an inventory of $k$ identical items. Minimum reservation cost per item is $c$ (Private to $S$).
* Buyer ($B$): Wants up to $k$ items. Maximum valuation per item is $v$ (Private to $B$).
* Rounds ($t$): The environment runs for a maximum of $T$ rounds ($t = 1, 2, \dots, T$).
* Discount Factor ($\delta$): A decay parameter $0 < \delta < 1$. Delays reduce payoff; a payout in round $t$ is multiplied by $\delta^{t-1}$.
* Terminal Rule: If no agreement is reached by $t = T$, the environment terminates with zero utility for both agents.

------------------------------
## 2. State Space Variables
At any point in the protocol, the state is tracked by the vector:
$$\sigma_t = (q_{\text{sold}}, \vec{P}_t, t)$$ 

* $q_{\text{sold}}$: Cumulative items sold so far (initialized to $0$).
* $\vec{P}_t$: Vector of agreed-upon strike prices for completed transactions.
* $t$: Current protocol time step.

------------------------------
## 3. Step-by-Step Instructions for Agents

[Round t = 1]
   │
   ▼
┌────────────────────────┐
│  Seller Offers (p_t)   │◄────────────────┐
└────────────────────────┘                 │
   │                                       │
   ▼                                       │
┌────────────────────────┐                 │
│  Buyer Evaluates       │                 │
└────────────────────────┘                 │
   │                                       │
   ├─► [ACCEPT] ──► Execute Trade ───────┐ │
   │                Update Inventory     │ │
   │                If q_sold < k ───────┼─┘ (Next round, S offers again)
   │                If q_sold == k ──► [END]
   │                                       │
   └─► [REJECT] ──► Increment t ───────────┤
                    If t > T ────────► [END] (Zero Utility)

## Instructions for Seller ($S$)

   1. Initialization: Set counter $t = 1$ and available inventory $q_{\text{rem}} = k$.
   2. Action Phase (Odd Rounds or Every Round depending on choice - here, Seller-led alternating): At the start of round $t$, calculate your target price $p_t$. Transmit the message $\text{Offer}(p_t)$ to the Buyer.
   3. Execution Phase:
   * If Buyer replies $\text{Accept}$, record transaction price $P_{\text{match}} = p_t$. Decrement inventory: $q_{\text{rem}} \leftarrow q_{\text{rem}} - 1$.
      * If Buyer replies $\text{Reject}$, increment $t \leftarrow t + 1$.
   4. Loop Criteria: If $q_{\text{rem}} > 0$ and $t \le T$, return to Step 2. Otherwise, terminate.

## Instructions for Buyer ($B$)

   1. Initialization: Set counter $t = 1$ and remaining demand $d = k$.
   2. Listening Phase: Receive $\text{Offer}(p_t)$ from the Seller.
   3. Evaluation Phase: Compare the proposed price $p_t$ against your private valuation $v$.
   * Condition to Accept: If $v \ge p_t$, evaluate if the strategic continuation payoff of waiting is lower than immediate acceptance. If yes, transmit $\text{Accept}$. Decrement demand: $d \leftarrow d - 1$.
      * Condition to Reject: If $v < p_t$, or if strategically holding out yields higher discounted future surplus, transmit $\text{Reject}$.
   4. Loop Criteria: If $d > 0$ and $t \le T$, increment $t \leftarrow t + 1$ and return to Step 2. Otherwise, terminate.

------------------------------
## 4. Utility and Payoff Mechanics
When a transaction for a single unit occurs at round $t$ at price $p_t$:
$$\text{Buyer Utility Space:} \quad U_B = \delta^{t-1} (v - p_t)$$ 
$$\text{Seller Utility Space:} \quad U_S = \delta^{t-1} (p_t - c)$$ 
For multi-item completions, the total utilities are the sum of the discounted surpluses across all $k$ items:
$$U_{B,\text{total}} = \sum_{i=1}^{k_{\text{actual}}} \delta^{t_i - 1} (v - P_i), \quad U_{S,\text{total}} = \sum_{i=1}^{k_{\text{actual}}} \delta^{t_i - 1} (P_i - c)$$ 
------------------------------
## 5. Protocol Constraints & Guardrails

* No Price Memory Overwrite: Once an item is sold at a specific strike price $P_i$, that transaction is locked and cannot be renegotiated in later rounds.
* Information Leakage Boundary: The protocol engine prohibits the Buyer from querying the value of $c$, and prohibits the Seller from querying the value of $v$. All learning must happen via inference from rejected/accepted offers.
* Strict Monotonicity Constraint (Optional Optimization): To force faster convergence, the protocol can enforce that $p_t \le p_{t-1}$ for all successive seller offers, penalizing the seller for demanding high margins indefinitely.

------------------------------

