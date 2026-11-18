-- =============================================================================
-- recon_check_solutions.sql  -  SPOILERS. Try tools/recon_check.sql first.
-- =============================================================================
-- Run:  sqlite3 db/trading.db < solutions/recon_check_solutions.sql
-- Expected results below are for the default seed (python db/seed_db.py).
-- =============================================================================


-- -----------------------------------------------------------------------------
-- SOLUTION 1: Orphaned order            Expected: ORD-100021 (Halcyon, ES futures)
-- -----------------------------------------------------------------------------
-- Anti-join: orders still PendingNew, never got an exchange OrderID (tag 37),
-- and have no fills. LEFT JOIN + "right side IS NULL" is the classic way to
-- find rows that are missing from another table.
SELECT o.order_id,
       o.client_id,
       o.symbol,
       o.quantity,
       o.ord_status,
       o.created_ts,
       ROUND((julianday('2026-11-17 21:00:00') - julianday(o.created_ts)) * 24 * 60, 1) AS minutes_stuck
FROM orders o
LEFT JOIN executions e ON e.order_id = o.order_id
WHERE o.ord_status = 'A'
  AND o.exchange_order_id IS NULL
  AND e.order_id IS NULL;
-- Why a rejected order (status 8) is not an orphan: the broker DID answer.
-- Why a working order (status 0) is not an orphan: it was acknowledged.


-- -----------------------------------------------------------------------------
-- SOLUTION 2: Fill quantity mismatch    Expected: ORD-100025 (real) + ORD-100020 (side-effect)
-- -----------------------------------------------------------------------------
-- LEFT JOIN + COALESCE so an order with filled_qty > 0 but no executions at all
-- would still show up as a mismatch (SUM would be NULL otherwise).
SELECT o.order_id,
       o.ord_status,
       o.quantity,
       o.filled_qty                          AS oms_filled_qty,
       COALESCE(SUM(e.last_qty), 0)          AS executed_qty,
       o.filled_qty - COALESCE(SUM(e.last_qty), 0) AS difference,
       COUNT(e.row_id)                       AS fill_rows
FROM orders o
LEFT JOIN executions e ON e.order_id = o.order_id
GROUP BY o.order_id
HAVING o.filled_qty <> COALESCE(SUM(e.last_qty), 0);
-- ORD-100025: OMS says 7,500 filled, broker only reported fills of 3,500 + 2,500 = 6,000.
--             1,500 shares are missing from the ledger: either a lost ExecutionReport or an
--             OMS bug. Check the FIX log (cum_qty on the last 35=8 is 6,000) and the broker.
-- ORD-100020: executed_qty is DOUBLE the order - that is the duplicate ExecID from Exercise 3,
--             not a genuine fill break. Fixing the duplicate fixes this row.


-- -----------------------------------------------------------------------------
-- SOLUTION 3: Duplicate execution ID    Expected: X5530050 on ORD-100020
-- -----------------------------------------------------------------------------
SELECT exec_id,
       order_id,
       COUNT(*)       AS times_booked,
       SUM(last_qty)  AS qty_booked,
       MIN(exec_ts)   AS first_booked,
       MAX(exec_ts)   AS second_booked
FROM executions
GROUP BY exec_id, order_id
HAVING COUNT(*) > 1;

-- Follow-up: true filled quantity once duplicates are removed (one row per exec_id).
SELECT o.order_id,
       o.filled_qty                 AS oms_filled_qty,
       SUM(d.last_qty)              AS deduped_executed_qty
FROM orders o
JOIN (SELECT DISTINCT exec_id, order_id, last_qty FROM executions) d ON d.order_id = o.order_id
WHERE o.order_id IN (SELECT order_id FROM executions GROUP BY exec_id HAVING COUNT(*) > 1)
GROUP BY o.order_id;
-- Deduped = 5,000 = orders.filled_qty, so the OMS order is right and the ledger is wrong.
--
-- Follow-up 2:  grep X5530050 logs/fix_gateway.log
--   The second message carries 43=Y (PossDupFlag) and 122=OrigSendingTime, with the SAME 34
--   (MsgSeqNum) and 17 (ExecID) as the original. It was replayed because the OMS sent a
--   ResendRequest (35=2) after the 15:41 disconnect with a stale sequence-number checkpoint.
--   Root cause: the execution handler did not de-duplicate on ExecID / ignore PossDup=Y
--   messages it had already processed.


-- -----------------------------------------------------------------------------
-- SOLUTION 4: Settlement break          Expected: ORD-100019 (JPM, 245.80 vs 245.08)
-- -----------------------------------------------------------------------------
SELECT s.settlement_id,
       s.order_id,
       s.settle_status,
       o.avg_px                                              AS executed_px,
       s.price                                               AS settled_px,
       ROUND(s.price - o.avg_px, 6)                          AS px_diff,
       ROUND(o.filled_qty * o.avg_px * o.multiplier, 2)      AS executed_notional,
       s.notional                                            AS settled_notional,
       ROUND(s.notional - o.filled_qty * o.avg_px * o.multiplier, 2) AS notional_diff
FROM settlements s
JOIN orders o ON o.order_id = s.order_id
WHERE ABS(s.price - o.avg_px) > 0.000001
   OR ABS(s.notional - o.filled_qty * o.avg_px * o.multiplier) > 0.01;
-- 245.80 settled as 245.08: the last two digits are transposed - a classic manual-keying
-- error. On 3,000 shares that is a USD 2,160 break the custodian would reject.


-- -----------------------------------------------------------------------------
-- SOLUTION 5: Credit-limit rejection    Expected: ORD-100037 (Pinnacle, NVDA)
-- -----------------------------------------------------------------------------
SELECT o.order_id,
       c.client_name,
       o.symbol,
       o.quantity,
       o.limit_price,
       o.quantity * o.limit_price * o.multiplier                      AS order_notional,
       c.credit_limit_usd,
       o.quantity * o.limit_price * o.multiplier - c.credit_limit_usd AS excess_over_limit,
       o.reject_reason
FROM orders o
JOIN clients c ON c.client_id = o.client_id
WHERE o.ord_status = '8';
-- Notional 28.5m vs limit 25.0m -> over by 3.5m. A 10% bump to 27.5m would NOT be enough,
-- so the reject was correct: nothing to fix on the broker side; the client / sales coverage
-- needs to request a limit increase from credit.
