# Trade Support & Exchange Operations Sandbox

A small, self-contained environment for practising the day-to-day work of a trade support / production support
analyst on a multi-asset desk: reconciling orders against executions and settlements in SQL, reading raw
**FIX 4.2** traffic, and triaging Linux-style gateway logs.

No frameworks, no web UI, no dependencies - Python standard library, SQLite and flat log files.

One trading day (**2026-11-17, UTC**), 8 institutional clients, 51 orders across **Equities, FX and Derivatives**
(futures and options), and **five production issues** hidden in the data for you to find.

---

## Architecture

```
                    python db/seed_db.py   (fixed random seed -> identical output every run)
                               |
        +----------------------+-----------------------+
        v                      v                       v
  db/trading.db       logs/fix_gateway.log     logs/system_errors.log
  (SQLite)            (raw FIX 4.2, 215 msgs)  (syslog-style gateway/OMS/kernel)
  clients                    |                       |
  orders  <------ same ClOrdIDs / ExecIDs / timestamps ------>
  executions                 |                       |
  settlements                v                       v
        ^             tools/fix_parser.py      grep / awk / sort / uniq
        |             (decode tag=value)       (Linux log triage)
  tools/recon_check.sql
  (SQL reconciliation exercises)  -->  solutions/  (check your work here)
```

The three outputs describe the *same* day. An order you find with SQL has a message trail in the FIX log and, if
something went wrong, a matching line in the system log. Following one `ClOrdID` across all three is the core skill.

### Message flow modelled

```
 Clients -> ACME_OMS ==FIX 4.2 session==> XBROKER (broker / venues)

  35=D  NewOrderSingle      OMS -> broker        35=F  OrderCancelRequest  OMS -> broker
  35=8  ExecutionReport     broker -> OMS        35=9  OrderCancelReject   broker -> OMS
        (ack 39=0, fills 39=1/2, cancel 39=4, reject 39=8)
  35=3  Reject (session level)                   35=A/5/0/1/2/4  Logon, Logout, Heartbeat,
                                                                 TestRequest, ResendRequest, SequenceReset
```

### Database

| Table         | Rows | What it holds |
|---------------|------|---------------|
| `clients`     | 8    | Institutional clients with a `credit_limit_usd` used for pre-trade checks |
| `orders`      | 51   | OMS order state: FIX status codes (`ord_status` = tag 39), `filled_qty`, `avg_px`, `exchange_order_id` |
| `executions`  | ~75  | One row per fill reported by the broker (ExecID, last qty/px). No unique constraint on `exec_id` - on purpose |
| `settlements` | ~38  | One row per fully-filled order: settle date (T+1, FX spot T+2), price, notional |

`orders.side`, `ord_status` and `order_type` store raw FIX codes (`1`=Buy, `2`=Filled, ...) - the code legend is at
the top of `tools/recon_check.sql`.

---

## Repository layout

```
db/
  seed_db.py            builds trading.db AND both log files, then self-checks the injected anomalies
  trading.db            generated
logs/
  fix_gateway.log       generated - raw FIX 4.2, '|' shown in place of SOH
  system_errors.log     generated - latency spikes, disconnects/reconnects, NTP, kernel noise
tools/
  fix_parser.py         CLI: raw FIX message / log file -> readable tag/value pairs
  recon_check.sql       the 5 SQL exercises (empty - write your answers here)
solutions/
  recon_check_solutions.sql   worked SQL answers + expected results
  log_triage_solutions.md     grep/awk answers
```

---

## Quick start

Requires Python 3.8+ (standard library only). Examples use forward slashes; they also work in PowerShell.

### 1. Generate the database and logs

```bash
python db/seed_db.py
```

This (re)creates `db/trading.db`, `logs/fix_gateway.log` and `logs/system_errors.log`, prints row/line counts and
runs a self-check that all five anomalies were injected. It is safe to re-run at any time; output is deterministic.
The anomaly order IDs are intentionally not printed. If you get stuck: `python db/seed_db.py --answer-key`.

### 2. Decode FIX messages

```bash
# A single raw message (| or SOH or ^A as the delimiter)
python tools/fix_parser.py "8=FIX.4.2|9=216|35=8|49=XBROKER|56=ACME_OMS|34=56|52=20261117-15:40:31.819|37=EQ7000020|11=ORD-100020|17=X5530050|20=0|150=2|39=2|1=C001|55=MSFT|54=1|38=5000|32=5000|31=428.35|30=XNAS|14=5000|6=428.35|151=0|60=20261117-15:40:31.819|10=184|"

# A whole log, one summary line per message (great for scanning)
python tools/fix_parser.py logs/fix_gateway.log --compact

# Everything for one order, fully decoded
python tools/fix_parser.py logs/fix_gateway.log --filter 11=ORD-100020

# All broker rejects (35=8 AND 39=8); filters are repeatable and ANDed
python tools/fix_parser.py logs/fix_gateway.log --filter 35=8 --filter 39=8 --compact

# Pipe from grep (use - for stdin)
grep ORD-100020 logs/fix_gateway.log | python tools/fix_parser.py -
```

Sample output:

```
  35    MsgType              8  ->  ExecutionReport
  150   ExecType             2  ->  Fill
  39    OrdStatus            2  ->  Filled
  54    Side                 1  ->  Buy
  55    Symbol               MSFT
  ...
  [integrity] BodyLength and CheckSum OK
```

Every message is also verified against its own `BodyLength (9)` and `CheckSum (10)`, so a hand-edited or truncated
message is flagged. The tool also understands `150=F` (Trade) because many real venues send it even on 4.2 sessions,
although strict FIX 4.2 reports fills as `150=1` (partial) / `150=2` (fill); the sandbox log uses the strict 4.2 values.

### 3. Run SQL

```bash
sqlite3 db/trading.db < solutions/recon_check_solutions.sql      # if the sqlite3 CLI is installed
python -m sqlite3 db/trading.db                                  # Python 3.12+: interactive shell, one statement at a time
python -m sqlite3 db/trading.db "SELECT ord_status, COUNT(*) FROM orders GROUP BY 1;"
```

Any SQLite GUI (DBeaver, DB Browser for SQLite, the VS Code SQLite extension) also works - just open `db/trading.db`.

### 4. Linux log triage on Windows

The `grep`/`awk` exercises need a Unix shell: use **WSL**, or **Git Bash** (ships with Git for Windows).

---

## Practice scenarios

Work through them in order. Try each one yourself before opening `solutions/`.

### SQL reconciliation (`tools/recon_check.sql`)

- [ ] **1. Orphaned order** - an order was sent to market but never acknowledged or executed. Find it with an anti-join; work out how long it has been stuck.
- [ ] **2. Partial-fill mismatch** - find orders where `orders.filled_qty` disagrees with `SUM(executions.last_qty)`. Explain every row you get back.
- [ ] **3. Duplicate execution ID** - find an `exec_id` booked twice, compute the true filled quantity, and say why it was booked twice.
- [ ] **4. Settlement discrepancy** - find the settled trade whose price/notional does not match what was executed, and name the likely keying error.
- [ ] **5. Broker rejection** - find the `OrdStatus=8` order, compare its notional with the client's credit limit, and decide whether the reject was correct.

### Linux log triage (grep / awk)

Answers and expected output: `solutions/log_triage_solutions.md`.

- [ ] **6. Trace one order across both logs** - take the orphan's `ClOrdID` from exercise 1 and `grep` both logs. Prove from the FIX log that no `35=8` ever came back, and find the matching `PENDING_NEW` warnings in the system log.
- [ ] **7. Message census** - count FIX messages by `MsgType` (`grep -o` + `sort | uniq -c`). Which types are present and in what proportion?
- [ ] **8. Find the replays** - list every message with `43=Y` (PossDupFlag). Which is the duplicate fill? Find the `35=2` ResendRequest and `35=4` GapFill around it.
- [ ] **9. Duplicate ExecID from the raw log** - extract every tag `17`, then use `sort | uniq -d` to find the repeated one without touching the database.
- [ ] **10. Latency spike analysis** - use `awk` on `system_errors.log` to calculate count, average and max `ack_latency_ms`; identify the time window and the root-cause lines that precede it.
- [ ] **11. Session outage timeline** - build a timeline of `DISCONNECTED` / `Reconnect` / `Logon acknowledged` events. Use `awk` to compute the downtime of the long outage. Which kernel message hints at the cause?
- [ ] **12. Session-level rejects** - find both `35=3` messages, read the `373` reason codes with `fix_parser.py`, and relate one of them to the `chronyd` entries in the system log.
- [ ] **13. Broker reject trail** - find the `39=8` ExecutionReport in the FIX log, read its `103` and `58` fields, and tie it back to the SQL result from exercise 5.
- [ ] **14. Stretch** - for every order in `trading.db` with status `2`, check that the FIX log contains a `39=2` report. Which shell/SQL combination would you use? What would a missing one mean?

---

## What each issue looks like in the real world

| # | Anomaly | Typical root causes |
|---|---------|---------------------|
| 1 | Orphaned order (no ack, no fill) | Message lost on a dying TCP session, application gap-filled over it, broker never saw it |
| 2 | `filled_qty` != sum of fills | Lost ExecutionReport, manual OMS amendment, out-of-order processing |
| 3 | Duplicate ExecID | Replay (`43=Y`) after reconnect not de-duplicated by the booking process |
| 4 | Settled price/notional break | Manual keying error, transposed digits, stale price on the confirmation |
| 5 | Broker reject, credit limit | Pre-trade risk check; limit needs amending by credit, not a technical fault |

## Resetting

`python db/seed_db.py` deletes and rebuilds everything, so you can experiment freely (including deleting rows or
editing logs) and restore the original state in a second.
