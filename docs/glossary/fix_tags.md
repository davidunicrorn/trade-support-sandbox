# FIX 4.2 Glossary - tags, messages and session concepts

The tags and values you are most likely to be asked about in a trade support interview, with where each one shows up
in this sandbox. Raw FIX is `tag=value` pairs separated by SOH (0x01), shown as `|` in the logs.
Decode any message with `python tools/fix_parser.py <message or logfile>`.

Interview habit: say the **name and the number** ("OrdStatus, tag 39") - people on a desk use both.

---

## 1. Message types (tag 35)

| 35 | Name | Direction (sandbox) | Purpose |
|----|------|---------------------|---------|
| D | NewOrderSingle | OMS -> broker | Send a new order |
| 8 | ExecutionReport | broker -> OMS | Ack, fill, partial fill, cancel confirm, reject - everything about an order's state |
| F | OrderCancelRequest | OMS -> broker | Ask to cancel a working order |
| 9 | OrderCancelReject | broker -> OMS | Cancel refused (typically "too late - already filled") |
| G | OrderCancelReplaceRequest | OMS -> broker | Amend an order (price/qty). Not in the sandbox data but expect it in interviews |
| 3 | Reject (session level) | either | Message malformed or breaks session rules; **not** an order reject |
| A | Logon | both | Start a session |
| 5 | Logout | both | End a session |
| 0 / 1 | Heartbeat / TestRequest | both | Keep-alive; TestRequest asks "are you there?" and expects a Heartbeat echoing tag 112 |
| 2 | ResendRequest | both | "I missed messages, resend from seq N" |
| 4 | SequenceReset | both | GapFill (123=Y) skips admin messages; Reset forces a new sequence number |

## 2. Header and trailer (every message)

| Tag | Name | Meaning / what to look for |
|-----|------|----------------------------|
| 8 | BeginString | FIX version, e.g. `FIX.4.2` |
| 9 | BodyLength | Bytes between tag 9 and tag 10. Wrong value = message was edited or corrupted |
| 35 | MsgType | See above |
| 49 / 56 | SenderCompID / TargetCompID | Who sent it / who it is for. Wrong CompID = session reject (373=9) |
| 34 | MsgSeqNum | Per-direction counter. Gaps trigger resend requests |
| 52 | SendingTime | UTC timestamp. Clock drift beyond tolerance gets rejected (373=10) |
| 43 | PossDupFlag | `Y` = this is a retransmission of an earlier message. **Must be de-duplicated, not re-processed** |
| 122 | OrigSendingTime | Time of the original message on a PossDup. Replays keep the original `34` and `17` |
| 10 | CheckSum | Sum of bytes mod 256, 3 digits |

## 3. Order identification

| Tag | Name | Notes |
|-----|------|-------|
| 11 | ClOrdID | **Client** order ID - the ID *you* assign; this is `orders.order_id` in the sandbox. Cancels and amends get a new ClOrdID |
| 41 | OrigClOrdID | On cancels/amends: the ClOrdID of the order being changed |
| 37 | OrderID | **Broker/exchange** order ID; assigned at ack. NULL in the sandbox until acknowledged |
| 17 | ExecID | Unique ID per execution report. Seeing the same 17 twice = duplicate (Exercise 3) |
| 1 | Account | Client / account the order is for |

Rule of thumb: 11 is yours, 37 is theirs, 17 is per event.

## 4. Instrument and order details

| Tag | Name | Typical values |
|-----|------|----------------|
| 55 | Symbol | `AAPL`, `EUR/USD`, `ES` |
| 167 | SecurityType | `CS` common stock, `FOR` FX, `FUT` future, `OPT` option |
| 107 | SecurityDesc | Free-text description |
| 200 / 201 / 202 | MaturityMonthYear / PutOrCall / StrikePrice | Derivatives: expiry, `0`=put `1`=call, strike |
| 54 | Side | `1` Buy, `2` Sell (also `5` Sell short, `6` Sell short exempt) |
| 38 | OrderQty | Original order size |
| 40 | OrdType | `1` Market, `2` Limit, `3` Stop, `4` Stop limit |
| 44 | Price | Limit price. Missing on a limit order -> session reject (371=44, 373=1) |
| 59 | TimeInForce | `0` Day, `1` GTC, `3` IOC, `4` FOK |
| 15 | Currency | Order currency |
| 60 | TransactTime | When the order/event happened |
| 100 | ExDestination | Where to route (MIC / venue) |
| 21 | HandlInst | `1` = automated, no broker intervention |

## 5. Execution report fields (35=8)

| Tag | Name | Meaning |
|-----|------|---------|
| 39 | OrdStatus | **Current state of the order**: see table below |
| 150 | ExecType | **What this report is about**: see table below |
| 20 | ExecTransType | `0` new (in FIX 4.2; removed in 4.3+) |
| 32 | LastShares (renamed LastQty later) | Quantity of *this* fill |
| 31 | LastPx | Price of *this* fill |
| 30 | LastMkt | Venue of the fill |
| 14 | CumQty | Total filled so far |
| 6 | AvgPx | Volume-weighted average price so far |
| 151 | LeavesQty | Still open = OrderQty - CumQty (0 if done/cancelled) |
| 58 | Text | Free text, e.g. the reject reason |
| 103 | OrdRejReason | Why an order was rejected: `1` unknown symbol, `2` exchange closed, `3` exceeds limit, `5` unknown order, `6` duplicate order |

### OrdStatus (39) vs ExecType (150) - a favourite question

**39 says where the order is now. 150 says what just happened.**
A partial fill arrives as `150=1`, `39=1`; the final fill as `150=2`, `39=2`.
A cancel confirmation is `150=4`, `39=4`. A reject is `150=8`, `39=8`.

| Code | OrdStatus (39) | ExecType (150) |
|------|----------------|----------------|
| A | PendingNew | PendingNew |
| 0 | New (acknowledged, working) | New |
| 1 | PartiallyFilled | PartialFill |
| 2 | Filled | Fill |
| 4 | Canceled | Canceled |
| 5 | Replaced | Replace |
| 6 | PendingCancel | PendingCancel |
| 8 | Rejected | Rejected |
| C | Expired | Expired |
| E | PendingReplace | PendingReplace |
| F | - | **Trade (FIX 4.3+ only)** - fills are `150=F` from 4.3 onwards; in 4.2 they are 1/2 |

**FIX version differences worth mentioning:** from 4.3 onwards fills are `150=F` (and partial/full fill state lives in OrdStatus);
`ExecTransType (20)` is gone; `LastShares (32)` is renamed `LastQty` in later versions (same tag number); 5.0 splits session
(FIXT 1.1) from application layers. Many real counterparties still run 4.2/4.4.

## 6. Cancel rejects and session rejects

| Tag | Name | Values |
|-----|------|--------|
| 102 | CxlRejReason | `0` too late to cancel, `1` unknown order, `3` already pending cancel/replace |
| 434 | CxlRejResponseTo | `1` reject of a cancel, `2` reject of a cancel/replace |
| 45 | RefSeqNum | Sequence number of the message being rejected |
| 371 | RefTagID | The offending tag |
| 372 | RefMsgType | Type of the rejected message |
| 373 | SessionRejectReason | `0` invalid tag, `1` required tag missing, `5` value incorrect, `6` bad format, `9` CompID problem, `10` SendingTime accuracy |

**Order reject (35=8, 39=8) vs session reject (35=3)** - another favourite: an order reject means the broker
*understood* the order and said no (credit, symbol, market closed). A session reject means the message was invalid at
protocol level, so the order never reached the broker's book.

## 7. Session management

| Tag | Name | Notes |
|-----|------|-------|
| 98 | EncryptMethod | `0` = none |
| 108 | HeartBtInt | Seconds between heartbeats (30 in the sandbox) |
| 112 | TestReqID | Echoed back in the Heartbeat answering a TestRequest |
| 7 / 16 | BeginSeqNo / EndSeqNo | Range in a ResendRequest; `16=0` means "to infinity" |
| 36 | NewSeqNo | Next expected sequence number after a SequenceReset |
| 123 | GapFillFlag | `Y` = skip over messages (typically admin) rather than reset |
| 141 | ResetSeqNumFlag | `Y` on Logon = start both sequences back at 1 (usually daily) |
| 97 | PossResend | `Y` = *application* may have sent this before (a new msg with a new seq); different from 43 |

### Session recovery cheat sheet (the sandbox incident)

1. Counterparty goes silent -> after ~1.2x HeartBtInt send a **TestRequest**; no Heartbeat back -> disconnect.
2. Reconnect and Logon. Whoever sees a higher-than-expected `34` sends a **ResendRequest** (`35=2`).
3. Sender replays application messages with `43=Y` and the original `34`/`122`; admin messages are covered with a
   **GapFill** (`35=4`, `123=Y`).
4. Receiver must **de-duplicate on ExecID (17) / ignore PossDup it already processed**. If it doesn't, you get duplicate
   fills (Exercise 3). If the sender gap-fills over an *application* message that never left, you get an orphaned order
   (Exercise 1).

---

## Questions you should be able to answer

- What is the difference between ClOrdID, OrderID and ExecID?
- An order is stuck in PendingNew. What do you check, in what order?
- What is the difference between a session reject and an order reject?
- You see `43=Y` on a message. What does it mean, and what is the risk?
- Why would a cancel request come back as a 35=9?
- `CumQty` doesn't match the sum of fills in our system. Where do you look?
- Walk me through what happens after a FIX session disconnects and reconnects.
