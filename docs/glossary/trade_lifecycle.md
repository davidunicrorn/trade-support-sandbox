# Trade Lifecycle & Trade Support Glossary (London focus)

The vocabulary behind a trade support / exchange operations role, in the order a trade moves through the firm.
Where a term maps to the sandbox, it says so. Regulatory details change - treat dates as "check before quoting".

---

## 1. The big picture

```
Client order -> OMS -> pre-trade risk -> routing (EMS / SOR / algo / DMA) -> venue / broker
   -> execution -> trade capture -> allocation -> confirmation / affirmation
   -> clearing (CCP) -> settlement (CSD / custodian) -> reconciliation -> regulatory reporting
```

Trade support sits across all of it: first line for "where is my order / trade", and the person who finds and
fixes breaks between systems.

| Term | Meaning |
|------|---------|
| Buy-side | Asset managers, pension funds, hedge funds - the ones placing orders (the sandbox's clients) |
| Sell-side | Banks and brokers executing and clearing for clients |
| Broker-dealer | Firm that trades for clients (agency) and/or itself (principal) |
| Prime broker (PB) | Provides financing, custody and clearing to hedge funds; gives them access to many venues |
| Custodian | Holds client securities and settles trades (e.g. BNY, State Street) |
| Venue | Where trading happens: regulated market/exchange (LSE, Eurex, CME), MTF, OTF, systematic internaliser, dark pool |
| MIC | Market Identifier Code, 4 letters: `XLON` London, `XNAS` Nasdaq, `XCME`... (FIX tag 100/30) |
| CCP | Central counterparty (LCH, Eurex Clearing, CME Clearing) - steps in between buyer and seller |
| CSD | Central securities depository - the settlement ledger (Euroclear UK & International / CREST, Euroclear, Clearstream, DTC) |

## 2. Pre-trade and order handling

| Term | Meaning |
|------|---------|
| OMS | Order Management System - the system of record for orders and their state (ACME_OMS in the sandbox) |
| EMS | Execution Management System - tools for working and routing orders to venues |
| SOR | Smart Order Router - splits/routes an order across venues for best outcome |
| DMA | Direct Market Access - client orders go straight to a venue under the broker's risk controls |
| Algo (TWAP / VWAP / POV) | Execution algorithms that slice a big order over time/volume. VWAP = volume-weighted average price |
| Market / limit / stop order | Fill at best price now / at price or better / trigger at a price |
| TIF (Day, GTC, IOC, FOK) | How long an order lives. IOC = fill what you can immediately, cancel rest. FOK = all or nothing |
| Pre-trade risk check | Credit/position/fat-finger/price-collar checks before the order goes out. Failure = reject (Exercise 5) |
| Fat finger | An order with a clearly erroneous size or price; caught by collars and max-notional checks |
| Credit limit | Maximum exposure allowed for a client; breaching it gives `39=8`, `103=3` |
| Kill switch | Emergency control to cancel all working orders and block new ones for a client/desk/venue |
| Stuck / orphaned order | Order the OMS thinks is live or pending but the venue has no knowledge of (Exercise 1) |
| Amend vs cancel/replace | Changing a working order. A race: the order may fill before the cancel arrives ("too late to cancel", 35=9) |
| Partial fill | Only part of the quantity executed; the rest keeps working (39=1) |

## 3. Execution and trade capture

| Term | Meaning |
|------|---------|
| Execution / fill | A match at a venue, reported back as an ExecutionReport |
| Average price (VWAP of fills) | Quantity-weighted price of all fills on an order (`orders.avg_px`) |
| Trade capture | Booking the execution into the trade/position system - where duplicates and missing fills hurt |
| Drop copy | Read-only duplicate stream of execution reports, used for independent monitoring and reconciliation |
| Trade break | Two sides disagree on a trade detail (price, quantity, side, date, counterparty) |
| Bust / correct | Venue cancels or corrects a trade after the fact (FIX 4.3+: `150=H` / `150=G`) |
| Allocation | Splitting a block order across client accounts, usually at average price |
| Give-up | Trade executed by one broker but handed to another (e.g. PB) for clearing/settlement |

## 4. Post-trade: confirmation, clearing, settlement

| Term | Meaning |
|------|---------|
| Confirmation / affirmation | Counterparties agree trade details (via matching platforms or SWIFT). Mismatch = a break |
| SSI | Standing Settlement Instructions - where cash/securities should go; wrong SSI is a common fail cause |
| Clearing | Netting and guaranteeing trades via a CCP; **novation** replaces the original counterparties with the CCP |
| Margin (initial / variation) | Collateral posted to the CCP; variation margin moves daily with mark-to-market |
| Settlement | Final exchange of securities and cash |
| DvP | Delivery versus Payment - securities and cash move together, removing principal risk |
| Trade date (T) / settle date | Day of execution / day of delivery |
| Settlement cycle | US equities T+1 (since May 2024). UK/EU equities currently T+2, with a move to T+1 planned for October 2027. FX spot T+2 (USD/CAD T+1). The sandbox uses T+1 equities/derivatives, T+2 FX spot |
| Settlement fail | Delivery did not happen on time (missing stock/cash, wrong SSI, unmatched instruction). Chased daily; EU/UK CSDR brings cash penalties and buy-ins |
| Buy-in | Buyer purchases the securities in the market at the failing seller's cost |
| Pending / settled | Settlement status in the sandbox (`settle_status`) |
| CLS / PvP | FX settlement service that settles both currency legs together to remove Herstatt risk |
| Value date | FX/derivative term for the settlement date |
| Corporate action | Dividend, split, rights issue etc. - drives price/quantity changes and claims |

## 5. Reconciliation (the core trade support skill)

| Term | Meaning |
|------|---------|
| Recon | Comparing two sources of truth and explaining every difference |
| Break | A difference found in a recon; has an owner, an age and a root cause |
| Types of recon | Trade (OMS vs broker confirm), position (internal vs custodian), cash (internal vs bank/nostro), price (booked vs market/confirm) |
| Tolerance | Small difference deliberately ignored (rounding) - note the sandbox queries use one for floats |
| Ageing | How long a break has been open; old breaks escalate |
| Root cause categories | Keying error, missing message, duplicate, timing, static data, system bug, counterparty error |
| Notional | `quantity x price x multiplier` - the size of the trade in currency terms |
| Contract multiplier | Futures/options scale (ES = 50, SPX options = 100); forgetting it is a classic 50x error |

## 6. Products you may be asked about

| Product | Key points |
|---------|-----------|
| Equities (cash) | Lot sizes, tick sizes, auctions, T+1/T+2, corporate actions, short selling flags |
| FX spot/forward | Currency pairs (EUR/USD: base/quote), value dates, no central exchange, quantity in base currency, CLS |
| FX NDF | Non-deliverable forward settled in cash in a convertible currency |
| Futures (ETD) | Exchange traded, cleared by CCP, expiry months, daily margin, multiplier, roll |
| Options | Strike, expiry, call/put, premium, exercise/assignment, expiry processing |
| Fixed income | Clean vs dirty price, accrued interest, settlement T+1/T+2, often OTC/voice |
| OTC derivatives | Bilateral/cleared swaps; confirmations and lifecycle events dominate support work |

## 7. Regulation and compliance (London / UK-EU)

| Term | Meaning |
|------|---------|
| FCA / PRA | UK conduct regulator / prudential regulator |
| MiFID II / MiFIR (UK version) | Rules on transparency, best execution, transaction reporting, clock sync, record keeping |
| Transaction reporting | Report every executed trade to the FCA (via an ARM) by the next working day; errors must be corrected and escalated |
| ARM / APA | Approved Reporting Mechanism / Approved Publication Arrangement |
| Best execution | Duty to get the best result for the client; evidenced with data |
| Clock synchronisation (RTS 25) | Business clocks traceable to UTC within set tolerance - why the 35=3 SendingTime reject matters |
| EMIR | Derivatives clearing, margining and reporting rules (EU and UK versions) |
| CSDR | EU settlement discipline: penalties and buy-ins for fails |
| Market abuse (MAR) | Insider dealing/manipulation; support staff may need to escalate suspicious activity |
| LEI | 20-character Legal Entity Identifier, mandatory on reports |
| ISIN / SEDOL / RIC | Security identifiers; mismatches cause static data breaks |

## 8. How the support function works day to day

| Term | Meaning |
|------|---------|
| L1 / L2 / L3 | First line (triage, runbooks) / second line (investigation, fixes) / engineering |
| Severity (P1-P4) | Business impact scale; P1 = trading halted or client money/regulatory impact |
| Runbook | Documented steps for a known issue |
| RCA / post-mortem | Root cause analysis after an incident; includes timeline, impact, fix, prevention |
| SLA / OLA | Agreed response/resolution times with clients / between internal teams |
| Start-of-day / end-of-day | Session logons, static data load, checks / batch processes, recon, reports, logouts |
| Change freeze / release | Controlled deployment windows; support often verifies after go-live |
| Escalation | Who to call and when - front office, ops, dev, vendor, broker |

---

## Map to this sandbox

| Sandbox exercise | Lifecycle area | Real-world question |
|------------------|----------------|---------------------|
| 1 Orphaned order | Order handling, session recovery | Client says "my order never went" - is it live at the venue? |
| 2 Fill mismatch | Execution / trade capture | OMS position differs from broker - which fill is missing? |
| 3 Duplicate ExecID | Session replay, trade capture | Why is the position doubled after a reconnect? |
| 4 Settlement break | Confirmation / settlement | Custodian rejected the instruction - what was keyed wrong? |
| 5 Credit reject | Pre-trade risk | Why was a client order rejected and who can lift the limit? |

## Questions you should be able to answer

- Walk me through the life of an equity trade from order to settlement.
- What is a settlement fail, what causes it, and what happens if it isn't fixed?
- What is the difference between clearing and settlement?
- A client's order was rejected for exceeding its credit limit. What do you do?
- How would you reconcile OMS trades against broker confirms? What do you do with a break?
- What changes when the UK/EU moves to T+1?
- Give me an example of an incident you led or supported from detection to RCA. (Crypto platform incidents work well here - emphasise timeline, comms and prevention.)
