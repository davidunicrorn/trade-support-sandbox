# Log triage solutions (SPOILERS)

Run from the repository root in WSL or Git Bash. Order IDs below are for the default seed; they come from your
SQL answers (`solutions/recon_check_solutions.sql`). Real FIX logs use SOH (0x01) as the delimiter - here it is
shown as `|`, which is why the patterns escape it as `\|`.

Log formats:

```
fix_gateway.log     2026-11-17 15:40:30.000 [OUT] 8=FIX.4.2|9=192|35=D|...|10=220|
system_errors.log   Nov 17 15:41:48 fix-gw01 fixgateway[2281]: ERROR Session ... DISCONNECTED reason=HEARTBEAT_TIMEOUT
```

---

## 6. Trace one order across both logs (orphan ORD-100021)

```bash
grep -n ORD-100021 logs/*.log
```

Expected: exactly **one** line in `fix_gateway.log` (the outbound `35=D`, no `[IN ] 35=8` reply, ever) plus three
`PENDING_NEW` warnings in `system_errors.log` at 15:47, 15:52 and 15:57.

Prove the negative explicitly - any inbound message mentioning the order?

```bash
grep ORD-100021 logs/fix_gateway.log | grep -c '\[IN '        # -> 0
```

Decode it: `grep ORD-100021 logs/fix_gateway.log | python tools/fix_parser.py -`

What happened: the `35=D` was written at 15:41:13, but `nf_conntrack: table full, dropping packet` (kernel) had
already started dropping traffic at 15:40:44. The broker never saw the order. After the reconnect the broker sent
`35=2` ResendRequest for that sequence number and the OMS answered with a `35=4` **GapFill** - telling the broker
"skip it" instead of resending the order. The order is lost: it must be confirmed with the broker and re-sent or
cancelled manually.

---

## 7. Message census by MsgType

```bash
grep -o '|35=[^|]*|' logs/fix_gateway.log | sort | uniq -c | sort -rn
```

Typical result: `35=8` ExecutionReports dominate (acks + fills), then `35=D` orders, then a handful of `35=0`, `35=A`,
`35=F`, `35=9`, `35=3`, `35=5`, `35=2`, `35=4`, `35=1`.

Per direction:

```bash
grep -c '\[OUT\]' logs/fix_gateway.log
grep -c '\[IN \]' logs/fix_gateway.log
```

---

## 8. Find the replays (PossDupFlag)

```bash
grep '|43=Y|' logs/fix_gateway.log | python tools/fix_parser.py - --compact
```

Two messages: the `35=4` GapFill (OMS side, covering the lost orphan order) and the `35=8` **replayed fill of
ORD-100020 with ExecID X5530050**. Note both reuse the *original* `34=` sequence number and add
`122=OrigSendingTime`.

The recovery handshake:

```bash
grep -E '\|35=(2|4)\|' logs/fix_gateway.log | python tools/fix_parser.py - --compact
```

---

## 9. Duplicate ExecID from the raw log

```bash
grep -oE '\|17=[^|]+' logs/fix_gateway.log | cut -d= -f2 | sort | uniq -d
# -> X5530050
```

Then see both copies:

```bash
grep 'X5530050' logs/fix_gateway.log | python tools/fix_parser.py - --compact
```

The second one has `**POSSDUP**` in the compact view: same `17=`, same `34=`, `43=Y`.

---

## 10. Latency spike analysis

```bash
grep 'ack_latency_ms' logs/system_errors.log | head
```

Count, average and max in one pass:

```bash
awk -F'ack_latency_ms=' '/ack_latency_ms/ { split($2, a, " "); s += a[1]; if (a[1] > m) m = a[1]; n++ }
     END { printf "spikes=%d avg=%.0fms max=%dms\n", n, s/n, m }' logs/system_errors.log
```

Expected: 7 spikes between 15:02 and 15:07, avg ~2.0 s, max ~3.3 s (normal acks are single-digit milliseconds).
Time window and cause:

```bash
awk '$3 >= "15:01:00" && $3 <= "15:10:00"' logs/system_errors.log
```

Root cause lines: `oms-core ... pre-trade risk service slow: p99=2140ms` at 15:01:48, `outbound order queue depth=412`
at 15:01:49, and recovery at 15:09:31. The gateway itself was fine; the delay was upstream in the OMS risk check.

Cross-check against the FIX log (ack delay = time between a `35=D` and its first `35=8`):

```bash
grep ORD-100010 logs/fix_gateway.log | cut -c1-60
```

---

## 11. Session outage timeline

```bash
grep -E 'DISCONNECTED|Reconnect attempt|connect\(\) failed|Logon acknowledged' logs/system_errors.log
```

Downtime per outage - from `DISCONNECTED` to the next `Logon acknowledged`:

```bash
awk '/DISCONNECTED/ { split($3, t, ":"); down = t[1]*3600 + t[2]*60 + t[3]; at = $3 }
     /Logon acknowledged/ && down { split($3, t, ":"); up = t[1]*3600 + t[2]*60 + t[3];
                                    printf "down %s -> up %s = %ds\n", at, $3, up - down; down = 0 }' logs/system_errors.log
```

Expected:

```
down 15:41:48 -> up 15:42:09 = 21s     (heartbeat timeout; but traffic was already dead from ~15:40:44)
down 18:12:05 -> up 18:12:18 = 13s     (scheduled venue restart, clean logout)
```

Cause hint for the first outage:

```bash
grep kernel logs/system_errors.log
# nf_conntrack: table full, dropping packet   (x3, 15:40:44 - 15:41:03)
```

The *real* outage started ~90 seconds before the gateway noticed (last inbound heartbeat 15:40:42, TestRequest only at
15:41:18, timeout at 15:41:48). That detection gap is why an order could be "sent" into a dead socket.

---

## 12. Session-level rejects (35=3)

```bash
grep '|35=3|' logs/fix_gateway.log | python tools/fix_parser.py -
```

- 14:44:02, `373=10` **SendingTime accuracy problem** (`371=52`). Matching system log:
  `chronyd: Can't synchronise` at 14:21 and `System clock wrong by 2.41 seconds` at 14:44:03 - the OMS clock had
  drifted more than the allowed tolerance, the broker refused the message, and after NTP stepped the clock the order
  was resent (same ClOrdID) and filled normally.
- 16:20:11, `373=1` **Required tag missing**, `371=44` - a limit order sent without a Price. Resent with `44=` populated.

Neither is an order-level reject: the order never entered the broker's book, so there is no `39=8`.

---

## 13. Broker reject trail

```bash
grep -E '\|39=8\|' logs/fix_gateway.log | python tools/fix_parser.py -
grep 'OrdStatus=8' logs/system_errors.log
```

Expect `150=8`, `39=8`, `103=3` (OrderExceedsLimit) and `58=Pre-trade risk check failed: order notional USD
28,500,000.00 exceeds credit limit USD 25,000,000.00`. These match the 28.5m vs 25m SQL result from exercise 5.

---

## 14. Stretch: every Filled order has a 39=2 report

```bash
# Orders the database says are Filled
python -m sqlite3 db/trading.db "SELECT order_id FROM orders WHERE ord_status='2' ORDER BY 1;" \
  | tr -d "(),'\r" | sort > /tmp/db_filled.txt

# Orders the FIX log says were Filled (35=8 with 39=2 for a new-order ClOrdID)
grep '|39=2|' logs/fix_gateway.log | grep -o '|11=ORD-[0-9]*' | cut -d= -f2 | sort -u > /tmp/fix_filled.txt

# Differences either way
comm -3 /tmp/db_filled.txt /tmp/fix_filled.txt
```

Expected: no differences. A database "Filled" order missing from the FIX log would mean the OMS marked it filled
without a broker confirmation (a serious break); the reverse means a fill was reported but never booked.
