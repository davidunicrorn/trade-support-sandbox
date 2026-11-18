# Mock incident scenarios - practise saying it out loud

> **Spoiler warning:** these walk through the same five issues hidden in the sandbox, plus the latency spike. Do
> the SQL exercises (`tools/recon_check.sql`) and log triage (README items 6-14) first, then use this file to practise
> *explaining* what you found. Order IDs are deliberately left out - find them yourself.

Interviewers rarely ask "write me a query". They ask **"something broke - what do you do?"** What they are listening
for is not the perfect answer but a calm, structured one. This file gives you the structure and six worked examples.

---

## The framework: Clarify, Locate, Diagnose, Mitigate, Communicate, Prevent

Use it for every scenario. Saying the steps out loud shows you have a method rather than a hunch.

| Step | What you do | Why it matters |
|------|-------------|----------------|
| 1. Clarify | Who, which order/trade ID, which time, what did they expect vs see? | Wrong assumptions waste the most time. A precise ID turns a vague complaint into a query |
| 2. Locate | Find where in the lifecycle it stopped: client -> OMS -> FIX session -> broker -> fills -> settlement | A trade moves through many systems; the break is at ONE hand-off. Find the hand-off first |
| 3. Diagnose | Gather evidence from at least two independent sources (DB, FIX log, system log) | One source can lie or be stale. Two sources agreeing is a finding; two disagreeing is the finding |
| 4. Mitigate | Stop the harm now (re-send, cancel, correct booking), with the right approvals | Impact first, root cause second. Never fix production data without sign-off |
| 5. Communicate | Short, factual updates to the client and internal teams, with next update time | Silence feels like failure. Clients forgive problems, not surprises |
| 6. Prevent | Raise a ticket / RCA with the root cause and a concrete fix | This is what separates support from firefighting |

**A good client update has five parts:** what we see, impact, what we are doing, what we need from you, next update at
HH:MM. Example: *"We can see your order was sent at 15:41 but not acknowledged by the broker, so it is not live in the
market. We are confirming with the broker and will re-send or cancel on your instruction. Next update at 16:00."*

---

## Scenario 1 - "My order never went" (orphaned order)

**The call:** *A hedge fund says it sent a futures order at about 15:41 and has seen nothing since. Their screen shows
it as working.*

**Triage**
1. **Clarify:** order ID (ClOrdID), instrument, time, what status the client sees.
2. **Locate (DB):** is it stuck in PendingNew with no exchange order ID and no fills? Run Exercise 1's anti-join.
   `grep <ClOrdID> logs/*.log` for the trail.
3. **Diagnose (FIX log):** one outbound `35=D`, no inbound `35=8` at all. So the broker never acknowledged it.
4. **Diagnose (system log):** a session problem at the same time: kernel `nf_conntrack: table full, dropping packet`
   at 15:40:44, TestRequest at 15:41:18, heartbeat timeout and disconnect at 15:41:48, reconnect at 15:42:09.
5. **Recovery detail:** after reconnect the broker sent a ResendRequest covering that sequence number, and the OMS
   answered with a **GapFill (35=4)**, telling the broker "skip it". The order was lost in that gap.

**Likely cause:** the connection was already dead when the order was written; later, the gap-fill told the broker to
ignore the missing message instead of resending it.

**Mitigate:** confirm with the broker that they hold nothing for that ClOrdID. Then, on the client's instruction,
cancel in the OMS and re-send with a **new** ClOrdID (never reuse the old one). Check the market has not moved.

**Communicate:** tell the client straight away that the order is **not live**, even though their screen says it is.

**Prevent:** alert on orders stuck in PendingNew beyond N seconds (the system log only warned at 15:47, six minutes
later); review why the OMS gap-fills over application messages; fix the connection-tracking problem on the host.

**Why this is the right approach:** the dangerous belief is "my order is working". Proving it is *not* working from two
sources, then telling the client first, limits the damage. The prevention step addresses the detection delay, not just
this order.

**Interview angle:** *"It was a silent failure - the system reported nothing wrong. I would always ask what a monitor
could have caught earlier."*

---

## Scenario 2 - "Our position doesn't match yours" (fill mismatch)

**The call:** *An insurer's ops team says the broker confirms 6,000 shares bought on a 10,000-share order, but the
OMS shows 7,500.*

**Triage**
1. **Clarify:** order ID; which number is the client's, which is ours; are they comparing today's fills only?
2. **Locate (DB):** Exercise 2. `orders.filled_qty` 7,500 vs `SUM(executions.last_qty)` 6,000. The difference is 1,500.
3. **Diagnose (FIX log):** the last ExecutionReport shows `14=6000` (CumQty). The broker's view matches the ledger, not
   the OMS. There is no fill for the extra 1,500.
4. **Rule out:** a duplicate ExecID? A replay (43=Y)? A later fill we dropped? None here - this is a plain mismatch.

**Likely cause:** the OMS counter was changed without a matching ExecutionReport (a manual edit, a bug, or a lost
message that was applied from another path).

**Mitigate:** the broker's fills are the source of truth for what executed, so correct `filled_qty` to 6,000 through
the approved process (never ad hoc SQL in production) and check positions/risk built from the wrong number. The order
stays partially filled with 4,000 left.

**Communicate:** confirm to the insurer the order is 6,000 filled, remainder still working or cancelled on request.

**Prevent:** a daily recon of OMS fills vs broker/drop-copy fills; alert when `filled_qty` changes without an ExecID.

**Why this is the right approach:** with a disagreement you do not argue about who is right; you go to the primary
evidence, which is the execution reports from the venue.

---

## Scenario 3 - "Why is our position doubled?" (duplicate ExecID)

**The call:** *A fund bought 5,000 shares of a large-cap stock but risk shows 10,000.*

**Triage**
1. **Clarify:** order ID, expected vs shown quantity, when the doubling appeared.
2. **Locate (DB):** Exercise 3. The same `exec_id` booked twice, about 1m 38s apart. `orders.filled_qty` is 5,000 but
   the ledger sums to 10,000 (this is also the second row of Exercise 2's answer).
3. **Diagnose (FIX log):** `grep <ExecID>` shows two identical ExecutionReports. The second has **`43=Y`**
   (PossDupFlag) and `122=OrigSendingTime`, with the same `34` (MsgSeqNum) as the original.
4. **Diagnose (system log):** it came right after the 15:41 disconnect. The OMS sent a ResendRequest from a stale
   sequence checkpoint and the broker replayed the fill. The log even says "seq store checkpoint older than last
   processed inbound seq".

**Likely cause:** after recovery, replayed messages were processed as if new. The booking process should de-duplicate on
ExecID (and treat `43=Y` with suspicion), and the table has no unique constraint to stop it.

**Mitigate:** remove the duplicate row through the controlled process, recompute the position, and tell risk/PnL to
re-run. Check for *other* replays in the same window (`grep '43=Y'`) so you do not fix one and miss another.

**Prevent:** add a unique key on ExecID (or an idempotent insert), persist sequence numbers durably so a restart does
not need a conservative resend, and add a recon check for duplicate ExecIDs.

**Why this is the right approach:** a duplicate is a *symptom of the session recovery*, so fixing the row without asking
"where else did the replay land?" leaves the same problem elsewhere.

**Interview angle:** *"Idempotency. The message is allowed to arrive twice, but processing it twice must not change the
result."* This maps directly to retry-safe API calls in your crypto platform experience.

---

## Scenario 4 - "The custodian rejected our settlement instruction" (price break)

**The call:** *Settlement ops reports a stock trade will not match at the custodian. Price on the instruction looks
off.*

**Triage**
1. **Clarify:** trade/order ID, the two prices in dispute, the settlement date and status.
2. **Locate (DB):** Exercise 4. The settlement price differs from `avg_px`; so does the notional.
3. **Diagnose:** executed at 245.80, settled at 245.08. The last two digits are **transposed**. On 3,000 shares the
   notional is USD 2,160 too low. A difference divisible by 9 is a classic tell for a transposition.
4. **Rule out:** is the execution itself right? Compare with the FIX fill (`31=LastPx`). Yes - the break is
   downstream of the trade, in the settlement instruction.

**Mitigate:** correct and re-send the instruction before the settlement deadline (here T+1), because an unmatched
instruction is likely to **fail** and can attract penalties or a buy-in under CSDR-style rules. Check the cut-off times.

**Communicate:** inform the custodian and operations; confirm the corrected instruction has been sent and matched.

**Prevent:** generate settlement instructions from booked trades instead of manual keying, or add a pre-send validation
against the execution average price.

**Why this is the right approach:** the clock is the issue. Settlement has a hard deadline, so you correct first and
write the RCA afterwards. Also verify *upstream* (the fill) before blaming the instruction.

**Interview angle:** *"Operational risk. I would ask how many other manual entries exist and whether a control would
catch them."*

---

## Scenario 5 - "Why was our order rejected?" (credit limit)

**The call:** *A quant fund is angry that a 150,000-share order was rejected at about 19:03.*

**Triage**
1. **Clarify:** order ID, client, time. Do they know the reason code?
2. **Locate (DB/FIX):** Exercise 5. `OrdStatus=8` with `OrdRejReason=103=3` (OrderExceedsLimit) and a text saying the
   notional exceeds the credit limit.
3. **Diagnose:** 150,000 x 190.00 = USD 28,500,000 against a USD 25,000,000 limit, a USD 3.5m excess. A 10% limit
   increase to 27.5m would still not cover it.
4. **Decide:** was it a fault? No - the pre-trade control did exactly what it should.

**Mitigate / next step:** this is not a technical fix. Tell the client why, and offer options: send a smaller order
within the limit, or have sales/relationship management raise a limit request to credit. Only credit can lift it.

**Communicate:** be factual and non-defensive: the order breached the limit, here are the numbers, here is how to
proceed. Do not promise a limit increase.

**Prevent / improve:** make sure the client gets a clear reject message quickly; consider notifying the client when they
approach their limit.

**Why this is the right approach:** a good support analyst can separate "the system is broken" from "the control worked".
Over-engineering a fix here would be wasted effort; getting the client to the right team is the value you add.

**Careful with:** never override a risk control to please a client.

---

## Scenario 6 - "Everything felt slow around 15:05" (latency spike)

**The call:** *Several clients say their orders took seconds to confirm in the middle of the afternoon.*

**Triage**
1. **Clarify:** which clients, which window, which instruments? Is it still happening?
2. **Locate (system log):** `grep ack_latency_ms logs/system_errors.log` shows seven slow acks between 15:02 and 15:07,
   averaging about 2 s with a maximum near 3.3 s (normal is milliseconds).
3. **Diagnose:** the lines just before the first spike say the **pre-trade risk service was slow** (p99 2,140 ms) and the
   outbound queue was backed up (depth 412). Recovery is logged at 15:09.
4. **Rule out:** is it the broker or the network? The delay sits between sending the order and the first ExecutionReport,
   but the gateway lines show the OMS queue was the bottleneck. Heartbeats and the session were healthy.

**Mitigate:** nothing to do now as it recovered; confirm no orders were lost (every slow order eventually got a fill or ack).

**Communicate:** short note to clients: "a slowdown in our pre-trade risk check between 15:02 and 15:09; all orders were
processed; no action needed."

**Prevent:** alert on risk-service p99 and queue depth before clients notice; capacity-test the risk service; set up
a latency dashboard per hop (OMS, risk, gateway, broker) so the slow component is obvious.

**Why this is the right approach:** latency problems are about **where** the time goes. Measuring per hop beats
guessing. This is the same skill as tracing a slow request through microservices.

---

## Practising

- Pick a scenario, set a 2-minute timer, and say your answer out loud using the six steps. Record yourself once.
- Swap the scenario details (a different client, instrument or time) and answer again.
- For each, prepare **one question back** to the interviewer ("how do you monitor stuck orders here?"). It shows you
  think about prevention.
- Prepare **two of your own incidents** from your crypto operations experience in the same format: what broke, how you
  located it, how you communicated, what you changed afterwards.
