-- =============================================================================
-- recon_check.sql  -  START HERE: reconciliation exercises for trading.db
-- =============================================================================
-- Five core exercises plus three bonus exercises on reference (lookup) data.
-- Write your queries under each exercise. Try first, then compare with
-- solutions/recon_check_solutions.sql.
--
-- Run (pick one):
--   sqlite3 db/trading.db < tools/recon_check.sql
--   python -m sqlite3 db/trading.db          (Python 3.12+; interactive, paste one statement at a time)
--   or open db/trading.db in DBeaver / DB Browser for SQLite / the VS Code SQLite extension
--
-- SCHEMA CHEAT-SHEET
--   clients      (client_id, client_name, client_type, country, credit_limit_usd)
--   orders       (order_id, client_id, symbol, security_desc, asset_class, side,
--                 order_type, quantity, limit_price, filled_qty, avg_px, ord_status,
--                 currency, multiplier, venue, exchange_order_id, reject_reason,
--                 created_ts, updated_ts)
--   executions   (row_id, exec_id, order_id, exec_type, last_qty, last_px, venue, exec_ts)
--   settlements  (settlement_id, order_id, trade_date, settle_date, quantity, price,
--                 notional, currency, settle_status)
--   ref_ord_status (code, name, description, is_terminal)   <- lookup for orders.ord_status
--   ref_side       (code, name)                             <- lookup for orders.side
--   ref_order_type (code, name)                             <- lookup for orders.order_type
--
-- FIX CODES USED IN THE DATA
--   ord_status : A=PendingNew  0=New  1=PartiallyFilled  2=Filled  4=Canceled  8=Rejected
--   side       : 1=Buy  2=Sell          order_type: 1=Market  2=Limit
--   exec_type  : 1=PartialFill  2=Fill  (executions holds fills only)
--
-- NOTES
--   * order_id is the FIX ClOrdID (tag 11); exec_id is the FIX ExecID (tag 17).
--   * Notional = quantity * price * multiplier (multiplier is 1 for cash products,
--     50 for ES, 1000 for ZN/CL, 100 for SPX options).
--   * All timestamps are UTC, trade date 2026-11-17. Think of "now" as end of day.
--   * Every exercise has a matching trail in logs/fix_gateway.log and
--     logs/system_errors.log - once you find an order_id, go grep for it.
--
-- WHY THESE EXERCISES (use this when explaining them in an interview)
--   Trade support is largely "two sources disagree - which one is right and why?".
--   Each exercise is one of the five most common ways that happens: a missing message,
--   a quantity that does not add up, a duplicate, a value that was keyed wrong
--   downstream, and a control that fired correctly. The SQL patterns (anti-join, GROUP BY
--   ... HAVING, join with tolerance) are the ones you will reuse daily.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- EXERCISE 1: The orphaned order
-- -----------------------------------------------------------------------------
-- An order was sent to the broker but never acknowledged (no 35=8 came back)
-- and never executed. Find it using only the database.
-- Think: what status does the OMS leave it in? Is there an exchange order id?
-- Are there any rows in executions for it? Which table join finds "missing" rows?
-- Then: how long has it been stuck, and which client is affected?
--
-- WHY: an unacknowledged order is the scariest silent failure - the client believes
-- it is working, the market has never seen it, and the price can run away.

-- your query here



-- -----------------------------------------------------------------------------
-- EXERCISE 2: Fill quantity mismatch (orders vs executions)
-- -----------------------------------------------------------------------------
-- For every order, the OMS keeps a running orders.filled_qty. The execution
-- ledger should agree with it. List every order where
-- SUM(executions.last_qty) <> orders.filled_qty, showing both numbers and the
-- difference. Don't forget orders that have filled_qty > 0 but NO execution rows.
-- You should get more than one row - decide which is the "real" partial-fill
-- break and which is a side-effect of something in Exercise 3.
--
-- WHY: positions and risk are built from fills. If the two numbers disagree, the
-- desk is trading against a wrong position until someone finds the break.

-- your query here



-- -----------------------------------------------------------------------------
-- EXERCISE 3: Duplicate execution ID
-- -----------------------------------------------------------------------------
-- exec_id has no UNIQUE constraint. Find every exec_id that appears more than
-- once, show the order it belongs to and both booking timestamps.
-- Follow-up: what is that order's TRUE filled quantity once the duplicate is
-- removed, and how does it compare with orders.filled_qty?
-- Follow-up 2: grep logs/fix_gateway.log for the ExecID. Which tag marks the
-- second message as a replay, and why did the broker resend it?
--
-- WHY: ExecID is the idempotency key of the execution feed. Duplicates double a
-- position and its P&L; finding the replay (43=Y) shows you understand the cause.

-- your query here



-- -----------------------------------------------------------------------------
-- EXERCISE 4: Settlement price / notional discrepancy
-- -----------------------------------------------------------------------------
-- Every Filled order (ord_status = '2') has a settlement row. Compare the
-- settlement with what was actually traded: price vs orders.avg_px and
-- notional vs filled_qty * avg_px * multiplier. Return only the breaks, with
-- the price difference and the notional difference (use a small tolerance for
-- floating-point and rounding noise).
-- Follow-up: eyeball the two prices - what kind of keying error is it?
--
-- WHY: a settlement break means real cash or securities are about to move at the
-- wrong amount; the custodian or CCP will reject or, worse, accept it.

-- your query here



-- -----------------------------------------------------------------------------
-- EXERCISE 5: Broker rejection - credit limit breach
-- -----------------------------------------------------------------------------
-- The broker rejected an order with OrdStatus=8 (FIX tag 39) / OrdRejReason=3.
-- Find all rejected orders and join to clients. Calculate the order notional
-- (quantity * limit_price * multiplier) and show it next to the client's
-- credit_limit_usd, plus by how much the limit was exceeded.
-- Follow-up: would the order have passed with a 10% larger credit limit?
--
-- WHY: not every exception is a fault. Support must tell "system broke" from
-- "control worked", explain it to the client, and route it to the right team.

-- your query here



-- =============================================================================
-- BONUS EXERCISES - reference (lookup) data
-- =============================================================================
-- WHY BONUS: in production most of your queries join a transaction table to
-- static data (instrument master, counterparties, status codes). Orders store raw
-- FIX codes, exactly as the wire does; the ref_* tables give them names.
-- Do these after the five core exercises.

-- -----------------------------------------------------------------------------
-- BONUS B1: Readable status counts
-- -----------------------------------------------------------------------------
-- Count orders per status, showing the code AND its name from ref_ord_status
-- (use a JOIN, not a CASE expression).
-- Think: why is a lookup table better than repeating a CASE block in every query?

-- your query here



-- -----------------------------------------------------------------------------
-- BONUS B2: Open-order blotter with names
-- -----------------------------------------------------------------------------
-- List every order that is still open (ref_ord_status.is_terminal = 0), showing
-- order_id, client_name, symbol, side NAME, order type NAME, status NAME, quantity
-- and filled_qty, oldest first. Join clients, ref_side, ref_order_type and
-- ref_ord_status.
-- Think: which of these open orders should worry you most?

-- your query here



-- -----------------------------------------------------------------------------
-- BONUS B3: Codes with no reference data
-- -----------------------------------------------------------------------------
-- Write a query that returns any orders.ord_status value with no row in
-- ref_ord_status (it should return nothing today). Then prove it works: inside a
-- transaction, set one order's status to 'Z', re-run the query, and ROLLBACK:
--     BEGIN;  UPDATE orders SET ord_status = 'Z' WHERE order_id = 'ORD-100001';
--     <your query>
--     ROLLBACK;
-- Think: why is there deliberately no FOREIGN KEY from orders to ref_ord_status?

-- your query here
