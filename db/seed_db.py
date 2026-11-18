#!/usr/bin/env python3
"""
seed_db.py - build the Trade Support Sandbox.

One run produces three artefacts that always agree with each other:

    db/trading.db            SQLite: clients, orders, executions, settlements
    logs/fix_gateway.log     raw FIX 4.2 traffic (SOH shown as '|') for those orders
    logs/system_errors.log   Linux/syslog-style gateway + OMS events

The data is a single trading day (2026-11-17, all times UTC), multi-asset
(Equities, FX, Derivatives), generated from a fixed random seed so every run
is identical. Five classic production-support anomalies are injected on top of
otherwise clean data - their order IDs are deliberately NOT printed, so you can
hunt for them yourself (use --answer-key if you get stuck):

    1. Orphaned order       sent to the broker, never acknowledged, never executed
    2. Partial-fill mismatch orders.filled_qty != SUM(executions.last_qty)
    3. Duplicate ExecID     a fill replayed after a session recovery, booked twice
    4. Settlement break     settled price/notional differs from what was executed
    5. Broker reject        OrdStatus=8, pre-trade credit-limit breach

Usage:
    python db/seed_db.py                # rebuild DB + logs
    python db/seed_db.py --answer-key   # ...and print the anomaly order IDs

WHY IT IS BUILT THIS WAY (design notes - useful when explaining the project)
    * One script writes the DB *and* both logs. Real incidents are only
      investigable because several systems tell the same story; generating
      everything from one in-memory model guarantees order IDs, ExecIDs,
      quantities and timestamps agree across all three artefacts.
    * Fixed random seed. The same run gives the same data, so the solutions and
      README can quote exact results, and you can compare notes with others.
    * Anomalies are injected into otherwise clean data. A trade support
      analyst's job is to spot the exception in thousands of normal rows, so the
      normal rows have to be genuinely clean (the self-check at the end proves
      each anomaly appears exactly as designed and nothing else is broken).
    * The anomalies share root causes, as in production: the dropped session at
      15:40 explains BOTH the orphaned order and the duplicate ExecID. Real
      breaks rarely arrive alone.
    * Plain standard library only (sqlite3, random, datetime): nothing to
      install, easy to read in an interview, runs anywhere.
"""

import argparse
import itertools
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "trading.db"
FIX_LOG = ROOT / "logs" / "fix_gateway.log"
SYS_LOG = ROOT / "logs" / "system_errors.log"

SOH = "\x01"                      # real FIX delimiter; logs show it as '|'
TRADE_DATE = datetime(2026, 11, 17)
AS_OF = datetime(2026, 11, 18)    # "today" when deciding SETTLED vs PENDING
OMS, BROKER = "ACME_OMS", "XBROKER"   # SenderCompID / TargetCompID
HOST = "fix-gw01"
rng = random.Random(20261117)     # fixed seed => reproducible data set (see "WHY" in the module docstring)
# All timestamps are UTC, as on a real FIX gateway: SendingTime (tag 52) must be UTC,
# and mixing local times is a classic cause of "the logs don't line up" confusion.


def at(h, m, s=0, ms=0):
    """Wall-clock time on the trade date."""
    return TRADE_DATE + timedelta(hours=h, minutes=m, seconds=s, milliseconds=ms)


def ms(n):
    return timedelta(milliseconds=n)


def iso(ts):
    return ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


def fix_time(ts):
    return ts.strftime("%Y%m%d-%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


def num(x, dp=6):
    """Format a number the way FIX likes it: no trailing zeros."""
    s = f"{x:.{dp}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def quantize(px, tick):
    return round(round(px / tick) * tick, 6)


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
# client_id, name, type, country, credit_limit_usd, tradable asset classes
# WHY these clients: a spread of buy-side types (pension, hedge fund, insurer,
# sovereign, corporate treasury...) because desks see very different flow from
# each: corporates only do FX, pension funds trade large equity blocks, etc.
# WHY Pinnacle's limit is only 25m: it is the client we push over its credit
# limit in anomaly 5. Everyone else has headroom so no other order is rejected.
CLIENTS = [
    ("C001", "Meridian Asset Management", "ASSET_MANAGER", "US", 500_000_000, ("EQUITY", "DERIVATIVE")),
    ("C002", "Northgate Pension Fund", "PENSION", "GB", 250_000_000, ("EQUITY", "FX")),
    ("C003", "Halcyon Macro Partners", "HEDGE_FUND", "US", 150_000_000, ("FX", "DERIVATIVE", "EQUITY")),
    ("C004", "Pinnacle Quant Fund", "HEDGE_FUND", "US", 25_000_000, ("EQUITY", "DERIVATIVE")),
    ("C005", "Sterling Insurance Group", "INSURER", "GB", 300_000_000, ("EQUITY", "FX")),
    ("C006", "Atlas Sovereign Wealth", "SOVEREIGN", "AE", 750_000_000, ("EQUITY", "FX", "DERIVATIVE")),
    ("C007", "Orion Proprietary Trading", "PROP_TRADER", "US", 75_000_000, ("EQUITY", "DERIVATIVE")),
    ("C008", "Kestrel Corporate Treasury", "CORPORATE", "DE", 100_000_000, ("FX",)),
]
CLIENT_LIMIT = {c[0]: c[4] for c in CLIENTS}


# WHY an Instrument record: every product-specific rule lives in one place -
# tick size (price granularity), contract multiplier, venue, FIX SecurityType.
# Getting these wrong is a real source of breaks: forgetting that one ES future
# is worth 50x its quoted price is the classic notional error (see
# docs/glossary/trade_lifecycle.md, "Contract multiplier").
@dataclass(frozen=True)
class Instrument:
    symbol: str          # FIX tag 55
    desc: str            # FIX tag 107
    asset_class: str     # EQUITY | FX | DERIVATIVE
    sec_type: str        # FIX tag 167: CS, FOR, FUT, OPT
    venue: str           # FIX tag 100 / 30
    ccy: str             # FIX tag 15
    px: float            # reference price for the day
    tick: float
    mult: int            # contract multiplier (1 for cash products)
    sizes: tuple         # typical order sizes
    unit: int            # smallest slice a fill can be split into
    usd_rate: float = 1.0   # ccy -> USD, only used for credit-limit sanity
    extra: tuple = ()       # extra FIX tags (maturity, put/call, strike)


INST = {i.desc: i for i in [
    # Equities (USD, US venues)
    Instrument("AAPL", "APPLE INC", "EQUITY", "CS", "XNAS", "USD", 230.40, 0.01, 1, (500, 1000, 2500, 5000), 100),
    Instrument("MSFT", "MICROSOFT CORP", "EQUITY", "CS", "XNAS", "USD", 428.15, 0.01, 1, (500, 1000, 2000, 5000), 100),
    Instrument("JPM", "JPMORGAN CHASE & CO", "EQUITY", "CS", "XNYS", "USD", 245.80, 0.01, 1, (1000, 3000, 5000, 10000), 100),
    Instrument("XOM", "EXXON MOBIL CORP", "EQUITY", "CS", "XNYS", "USD", 112.30, 0.01, 1, (2000, 5000, 10000), 100),
    Instrument("NVDA", "NVIDIA CORP", "EQUITY", "CS", "XNAS", "USD", 190.00, 0.01, 1, (1000, 2500, 5000, 10000), 100),
    Instrument("GS", "GOLDMAN SACHS GROUP", "EQUITY", "CS", "XNYS", "USD", 590.25, 0.01, 1, (500, 1000, 2000), 100),
    Instrument("KO", "COCA-COLA CO", "EQUITY", "CS", "XNYS", "USD", 70.10, 0.01, 1, (5000, 10000, 20000), 100),
    Instrument("PFE", "PFIZER INC", "EQUITY", "CS", "XNYS", "USD", 26.40, 0.01, 1, (10000, 25000, 50000), 100),
    # FX spot (quantity = base currency amount)
    Instrument("EUR/USD", "EURUSD SPOT", "FX", "FOR", "FXALL", "USD", 1.0850, 0.00001, 1, (1_000_000, 2_000_000, 5_000_000, 10_000_000), 250_000, 1.0),
    Instrument("GBP/USD", "GBPUSD SPOT", "FX", "FOR", "FXALL", "USD", 1.2950, 0.00001, 1, (1_000_000, 3_000_000, 5_000_000), 250_000, 1.0),
    Instrument("USD/JPY", "USDJPY SPOT", "FX", "FOR", "EBS", "JPY", 152.350, 0.001, 1, (2_000_000, 5_000_000, 10_000_000), 250_000, 1 / 152.35),
    Instrument("USD/CAD", "USDCAD SPOT", "FX", "FOR", "EBS", "CAD", 1.3950, 0.00001, 1, (1_000_000, 3_000_000, 5_000_000), 250_000, 1 / 1.395),
    Instrument("AUD/USD", "AUDUSD SPOT", "FX", "FOR", "FXALL", "USD", 0.6550, 0.00001, 1, (2_000_000, 5_000_000), 250_000, 1.0),
    # Derivatives (futures: tag 200 = maturity; options add 201 put/call, 202 strike)
    Instrument("ES", "E-MINI S&P 500 DEC26", "DERIVATIVE", "FUT", "XCME", "USD", 6850.25, 0.25, 50, (5, 10, 25, 50), 1, 1.0, ((200, "202612"),)),
    Instrument("ZN", "10Y T-NOTE DEC26", "DERIVATIVE", "FUT", "XCBT", "USD", 111.28125, 0.015625, 1000, (25, 50, 100, 250), 1, 1.0, ((200, "202612"),)),
    Instrument("CL", "WTI CRUDE JAN27", "DERIVATIVE", "FUT", "XNYM", "USD", 61.45, 0.01, 1000, (5, 10, 25, 50), 1, 1.0, ((200, "202701"),)),
    Instrument("SPX", "SPX DEC26 7000 CALL", "DERIVATIVE", "OPT", "XCBO", "USD", 85.50, 0.05, 100, (10, 25, 50, 100), 1, 1.0, ((200, "202612"), (201, "1"), (202, "7000"))),
    Instrument("SPX", "SPX DEC26 6500 PUT", "DERIVATIVE", "OPT", "XCBO", "USD", 62.30, 0.05, 100, (10, 25, 50, 100), 1, 1.0, ((200, "202612"), (201, "0"), (202, "6500"))),
]}
BY_SYMBOL = {}
for _i in INST.values():
    BY_SYMBOL.setdefault(_i.symbol, _i)


def inst(symbol, desc=None):
    return INST[desc] if desc else BY_SYMBOL[symbol]


# --------------------------------------------------------------------------- #
# Order specs: 5 anomaly orders + 2 session-reject cases + ~44 "normal" orders
# --------------------------------------------------------------------------- #
SPIKE_START, SPIKE_END = at(15, 2), at(15, 9)           # latency spike window
# WHY blackouts: no normal orders are created while the session is down or being
# recovered. Otherwise a random fill could land in the middle of the 15:40-15:42
# incident and muddy the story (and the sequence-number arithmetic in the FIX log).
BLACKOUTS = [(at(15, 33), at(15, 43, 30)),              # session drop / recovery
             (at(18, 9), at(18, 14))]                   # short venue restart


# WHY anomalies are hand-written rather than random: each one needs an exact,
# explainable cause and exact numbers (7,500 vs 6,000; 245.80 vs 245.08) so the
# solutions can name them and so you can tell the story in an interview.
def anomaly_specs():
    return [
        # 1. Orphan: sent during a silent TCP failure, never acked, never filled
        #    REAL-WORLD CAUSE: the socket was dead but nothing had noticed yet, then the OMS
        #    answered the broker's ResendRequest with a GapFill over the lost order.
        dict(ts=at(15, 41, 13, 400), client="C003", inst=inst("ES"), side="1", qty=40,
             otype="2", limit=6851.00, scenario="orphan"),
        # 2. Partial-fill mismatch: fills sum to 6,000 but OMS shows 7,500
        #    REAL-WORLD CAUSE: a lost ExecutionReport or a manual OMS edit; the position is
        #    overstated by 1,500 shares until someone reconciles against the broker.
        dict(ts=at(16, 5, 22, 120), client="C005", inst=inst("XOM"), side="1", qty=10_000,
             otype="2", limit=112.40, scenario="partial_mismatch",
             fill_plan=[(3500, 112.38), (2500, 112.36)]),
        # 3. Duplicate ExecID: filled just before the session dropped, replayed after
        #    REAL-WORLD CAUSE: after reconnect the broker replays the fill with PossDup=Y and
        #    the booking process does not de-duplicate on ExecID, so the position doubles.
        dict(ts=at(15, 40, 30), client="C001", inst=inst("MSFT"), side="1", qty=5000,
             otype="2", limit=428.40, scenario="dup_exec", fill_px=428.35, fill_delay=1.8),
        # 4. Settlement break: executed at 245.80, settled at 245.08 (digit transposition)
        #    REAL-WORLD CAUSE: manual keying of the confirmation/settlement instruction.
        #    Why a transposition: it is the textbook human error and is divisible-by-9 detectable.
        dict(ts=at(15, 27, 8, 340), client="C002", inst=inst("JPM"), side="1", qty=3000,
             otype="2", limit=245.85, scenario="price_discrepancy", fill_px=245.80),
        # 5. Broker reject: 150,000 x 190.00 = USD 28.5m vs Pinnacle's USD 25m limit
        #    REAL-WORLD CAUSE: pre-trade credit check working as designed - not a fault, but
        #    support still has to explain it to the client and route a limit request to credit.
        dict(ts=at(19, 3, 41, 770), client="C004", inst=inst("NVDA"), side="1", qty=150_000,
             otype="2", limit=190.00, scenario="risk_reject"),
        # Session-level rejects (35=3) that are resent and then fill normally.
        # WHY include them: they are NOT among the five anomalies. They teach you to tell an
        # order reject (39=8) from a session reject (35=3) and to avoid false alarms.
        dict(ts=at(14, 44, 2, 100), client="C006", inst=inst("AAPL"), side="2", qty=2500,
             otype="2", limit=230.55, scenario="filled_single", session_reject="time"),
        dict(ts=at(16, 20, 11, 250), client="C007", inst=inst("CL"), side="1", qty=10,
             otype="2", limit=61.50, scenario="filled_single", session_reject="price"),
    ]


def random_ts(i):
    lo, hi = ((7 * 3600 + 300, 20 * 3600 + 1800) if i.asset_class == "FX"
              else (14 * 3600 + 1860, 20 * 3600 + 2700))
    while True:
        ts = TRADE_DATE + timedelta(seconds=rng.uniform(lo, hi))
        ts = ts.replace(microsecond=ts.microsecond // 1000 * 1000)
        if not any(a <= ts <= b for a, b in BLACKOUTS):
            return ts


def normal_specs():
    # WHY a scenario mix: real days are mostly clean fills, with a tail of partials,
    # still-working orders, cancels and "too late to cancel" races. The anomalies must
    # hide among those, not stand out as the only unusual rows.
    scenarios = (["filled_single"] * 18 + ["filled_multi"] * 14 + ["partial_working"] * 3 +
                 ["working"] * 3 + ["cancelled"] * 4 + ["cancel_reject"] * 2)
    rng.shuffle(scenarios)
    specs = []
    for sc in scenarios:
        for _ in range(100):   # retry until the order fits comfortably inside the credit limit
            client = rng.choice(CLIENTS)
            i = rng.choice([x for x in INST.values() if x.asset_class in client[5]])
            qty = rng.choice(i.sizes)
            mid = quantize(i.px + rng.randint(-40, 40) * i.tick, i.tick)
            if qty * mid * i.mult * i.usd_rate <= 0.8 * client[4]:
                break
        else:
            raise RuntimeError("could not size order inside credit limit")
        side = rng.choice("12")
        otype = rng.choice(["1", "2", "2"])
        skew = rng.randint(0, 3) * i.tick
        limit = quantize(mid + skew if side == "1" else mid - skew, i.tick) if otype == "2" else None
        specs.append(dict(ts=random_ts(i), client=client[0], inst=i, side=side, qty=qty,
                          otype=otype, limit=limit, mid=mid, scenario=sc))
    # Cluster six orders inside the latency-spike window. WHY: a spike is only visible in
    # the logs if several orders are in flight while it happens; one slow ack is just noise.
    for n, spec in enumerate(specs[:6]):
        spec["ts"] = SPIKE_START + timedelta(seconds=10 + n * 55 + rng.uniform(0, 20))
        spec["ts"] = spec["ts"].replace(microsecond=spec["ts"].microsecond // 1000 * 1000)
    return specs


# --------------------------------------------------------------------------- #
# FIX 4.2 message plumbing
# --------------------------------------------------------------------------- #
events = []          # every FIX message, in the order we emit them
sys_lines = []       # (timestamp, formatted syslog line)


def emit(ts, direction, mtype, body=None, replay_of=None):
    """Queue a FIX message. MsgSeqNum is assigned later, once everything is time-sorted.

    WHY not number messages as we create them: orders are built one at a time, but on the wire
    their messages interleave. A real session numbers messages in the order they are SENT, so we
    sort by time first and number afterwards - otherwise the sequence would have bogus gaps.
    """
    ev = dict(ts=ts, dir=direction, type=mtype, body=body, replay_of=replay_of, seq=None)
    events.append(ev)
    return ev


def seq_of(ev):
    # WHY lazy: a Reject (35=3) or ResendRequest (35=2) quotes another message's sequence number,
    # which does not exist yet when we write the message. The lambda is evaluated at render time.
    return lambda: ev["seq"]


def render(ev):
    """Build a complete FIX 4.2 string with correct BodyLength(9) and CheckSum(10).

    WHY compute real 9 and 10 rather than fake them: tools/fix_parser.py validates both, so a
    genuine log lets you practise spotting tampered or truncated messages, and it behaves like
    production. BodyLength counts bytes after the 9= field up to the SOH before 10=; CheckSum is
    the sum of all preceding bytes modulo 256, calculated with the REAL SOH (value 1), not '|'.
    """
    orig = ev["replay_of"]
    body = ev["body"] if ev["body"] is not None else orig["body"]
    sender, target = (OMS, BROKER) if ev["dir"] == "OUT" else (BROKER, OMS)
    fields = [(35, ev["type"]), (49, sender), (56, target), (34, ev["seq"])]
    if orig:                                            # PossDup / resend semantics
        # A replay keeps the ORIGINAL MsgSeqNum (34) and body (so the same ExecID 17), and adds
        # 43=Y plus 122=OrigSendingTime. That is exactly what makes a duplicate detectable.
        fields += [(43, "Y"), (52, fix_time(ev["ts"])), (122, fix_time(orig["ts"]))]
    else:
        fields.append((52, fix_time(ev["ts"])))
    fields += list(body)
    payload = "".join(f"{t}={v() if callable(v) else v}{SOH}" for t, v in fields)
    msg = f"8=FIX.4.2{SOH}9={len(payload)}{SOH}{payload}"
    msg += f"10={sum(msg.encode()) % 256:03d}{SOH}"
    return msg.replace(SOH, "|")


def assign_sequence_numbers():
    # Each direction (OMS->broker, broker->OMS) has its own independent counter, as in real FIX.
    nxt = {"OUT": 0, "IN": 0}
    for ev in events:
        if ev["replay_of"]:
            ev["seq"] = ev["replay_of"]["seq"]      # resends reuse the original number
        else:
            nxt[ev["dir"]] += 1
            ev["seq"] = nxt[ev["dir"]]


PROCS = {"gw": "fixgateway[2281]", "oms": "oms-core[2310]", "chrony": "chronyd[611]",
         "kern": "kernel", "cron": "CRON[8841]"}


def sysline(ts, proc, level, msg):
    """Queue a syslog-style line. msg may be a callable so it can use sequence numbers assigned later."""
    sys_lines.append((ts, proc, level, msg))


def render_sysline(ts, proc, level, msg):
    msg = msg() if callable(msg) else msg
    lvl = f"{level:<5} " if level else ""
    return f"{ts:%b %d %H:%M:%S} {HOST} {PROCS[proc]}: {lvl}{msg}"


# --------------------------------------------------------------------------- #
# Order lifecycle -> FIX messages + DB rows
# --------------------------------------------------------------------------- #
exec_counter = itertools.count(5530001)
exch_counter = itertools.count(7000001)
executions = []      # DB rows (dicts)


def new_order_body(o, i, ts, with_price=True):
    b = [(11, o["id"]), (21, 1), (1, o["client"]), (55, i.symbol), (167, i.sec_type)]
    b += list(i.extra)
    b += [(107, i.desc), (54, o["side"]), (60, fix_time(ts)), (38, o["qty"]), (40, o["otype"])]
    if o["otype"] == "2" and with_price:
        b.append((44, num(o["limit"])))
    b += [(15, i.ccy), (59, 0), (100, i.venue)]
    return b


def er_body(o, i, clordid, exch_id, exec_id, etype, status, cum, avg, leaves, ts,
            last=None, orig=None, rej=None, text=None):
    b = [(37, exch_id), (11, clordid)]
    if orig:
        b.append((41, orig))
    b += [(17, exec_id), (20, 0), (150, etype), (39, status), (1, o["client"]),
          (55, i.symbol), (54, o["side"]), (38, o["qty"])]
    if last:
        b += [(32, last[0]), (31, num(last[1])), (30, i.venue)]
    b += [(14, cum), (6, num(avg)), (151, leaves), (60, fix_time(ts))]
    if rej is not None:
        b.append((103, rej))
    if text:
        b.append((58, text))
    return b


def split_qty(total, n, unit):
    units = total // unit
    n = min(n, units)
    if n <= 1:
        return [total]
    cuts = sorted(rng.sample(range(1, units), n - 1))
    edges = [0] + cuts + [units]
    return [(b - a) * unit for a, b in zip(edges, edges[1:])]


def plan_fills(o, i):
    sc, qty = o["scenario"], o["qty"]

    def one_px():
        if o.get("fill_px"):
            return o["fill_px"]
        if o["otype"] == "2":     # limit orders fill at or better than limit
            k = rng.randint(0, 3) * i.tick
            return quantize(o["limit"] - k if o["side"] == "1" else o["limit"] + k, i.tick)
        return quantize(o["mid"] + rng.randint(-3, 3) * i.tick, i.tick)

    if sc in ("working", "cancelled", "orphan", "risk_reject"):
        return []
    if sc == "partial_mismatch":
        return o["fill_plan"]
    if sc == "filled_multi":
        parts = split_qty(qty, rng.randint(2, 4), i.unit)
    elif sc == "partial_working":
        units = qty // i.unit
        done = max(1, min(units - 1, round(units * rng.uniform(0.3, 0.7))))
        parts = split_qty(done * i.unit, rng.randint(1, 2), i.unit)
    else:                         # single full fill
        return [(qty, one_px())]
    return [(p, one_px()) for p in parts]


def build_flow(o):
    i, sc = o["inst"], o["scenario"]
    cid = o["id"]
    row = dict(order_id=cid, client_id=o["client"], symbol=i.symbol, security_desc=i.desc,
               asset_class=i.asset_class, side=o["side"], order_type=o["otype"],
               quantity=o["qty"], limit_price=o["limit"], filled_qty=0, avg_px=None,
               ord_status="A", currency=i.ccy, multiplier=i.mult, venue=i.venue,
               exchange_order_id=None, reject_reason=None, created_ts=o["ts"], updated_ts=o["ts"])
    o["row"], o["fills"] = row, []

    # ---- 1. Submit (35=D), optionally bounced at session level (35=3) and resent
    send_ts = o["ts"]
    kind = o.get("session_reject")
    if kind:
        first = emit(send_ts, "OUT", "D", new_order_body(o, i, send_ts, with_price=(kind != "price")))
        tag, code, text = (52, 10, "SendingTime accuracy problem") if kind == "time" else \
                          (44, 1, "Conditionally required field missing")
        emit(send_ts + ms(55), "IN", "3",
             [(45, seq_of(first)), (371, tag), (372, "D"), (373, code), (58, text)])
        sysline(send_ts + ms(60), "gw", "WARN",
                lambda f=first, c=code, t=text, k=cid:
                f"Session-level Reject received RefSeqNum={f['seq']} RefMsgType=D "
                f"SessionRejectReason={c} ({t}) ClOrdID={k}")
        send_ts += timedelta(seconds=3.1)
        emit(send_ts, "OUT", "D", new_order_body(o, i, send_ts))
        sysline(send_ts + ms(2), "gw", "INFO", f"Resending rejected order ClOrdID={cid} after correction")
    else:
        o["d_event"] = emit(send_ts, "OUT", "D", new_order_body(o, i, send_ts))
    if kind is None and sc == "orphan":
        # WHY we stop here: the order was written to the socket (35=D logged) but no ExecutionReport
        # ever comes back, so the OMS leaves it in PendingNew (A) with no exchange order ID.
        return row                                    # <-- never acked: that's the anomaly

    # ---- 2. Broker response: reject or ack
    # WHY latency is modelled: acks normally take single-digit milliseconds. Inside the
    # 15:02-15:09 window the OMS pre-trade risk service is slow, so acks take 1-3 s. That is the
    # raw material for the latency-triage exercises (grep/awk on ack_latency_ms).
    spiking = SPIKE_START <= o["ts"] <= SPIKE_END
    latency = rng.randint(900, 3400) if spiking else rng.randint(3, 28)
    t = send_ts + ms(latency)
    if sc == "risk_reject":
        # A risk reject is an ORDER-level answer: ExecutionReport with 150=8 / 39=8 and
        # OrdRejReason 103=3 (exceeds limit). OrderID (37) is "NONE" because the broker never
        # booked the order - which is why exchange_order_id stays NULL in the database.
        notional = o["qty"] * o["limit"] * i.mult
        limit_usd = CLIENT_LIMIT[o["client"]]
        text = (f"Pre-trade risk check failed: order notional USD {notional:,.2f} exceeds "
                f"credit limit USD {limit_usd:,.2f}")
        emit(t, "IN", "8", er_body(o, i, cid, "NONE", f"X{next(exec_counter)}", 8, 8, 0, 0,
                                   0, t, rej=3, text=text))
        row.update(ord_status="8", reject_reason=text, updated_ts=t)
        sysline(t, "gw", "WARN", f"Order rejected by XBROKER ClOrdID={cid} OrdStatus=8 "
                                 f"OrdRejReason=3 (Pre-trade credit limit breach) client={o['client']}")
        return row

    exch_id = f"{'EQ' if i.asset_class == 'EQUITY' else 'FX' if i.asset_class == 'FX' else 'DR'}{next(exch_counter)}"
    row["exchange_order_id"] = exch_id
    emit(t, "IN", "8", er_body(o, i, cid, exch_id, f"X{next(exec_counter)}", 0, 0, 0, 0, o["qty"], t))
    row.update(ord_status="0", updated_ts=t)
    if spiking:
        sysline(t, "gw", "WARN", f"ack latency exceeded threshold ClOrdID={cid} "
                                 f"ack_latency_ms={latency} threshold_ms=250")

    # ---- 3. Fills
    plan = plan_fills(o, i)
    delay = o.get("fill_delay") or (rng.uniform(0.03, 0.4) if o["otype"] == "1" else rng.uniform(0.5, 40))
    cum, value = 0, 0.0
    t_fill = t + timedelta(seconds=delay)
    for n, (fq, fpx) in enumerate(plan):
        cum += fq
        value += fq * fpx
        avg = round(value / cum, 6)
        done = cum == o["qty"]
        etype = 2 if done else 1
        exec_id = f"X{next(exec_counter)}"
        ev = emit(t_fill, "IN", "8", er_body(o, i, cid, exch_id, exec_id, etype, etype, cum, avg,
                                             o["qty"] - cum, t_fill, last=(fq, fpx)))
        o["fills"].append(dict(exec_id=exec_id, order_id=cid, exec_type=str(etype), last_qty=fq,
                               last_px=fpx, venue=i.venue, exec_ts=t_fill))
        o["last_fill_event"] = ev
        row.update(ord_status=str(etype), filled_qty=cum, avg_px=avg, updated_ts=t_fill)
        t = t_fill
        t_fill += timedelta(seconds=rng.uniform(0.1, 6))

    # ---- 4. Cancels
    cx_id = f"{cid}-CXL"
    if sc == "cancelled":
        t_cx = t + timedelta(seconds=rng.uniform(20, 120))
        emit(t_cx, "OUT", "F", [(41, cid), (11, cx_id), (37, exch_id), (1, o["client"]), (55, i.symbol),
                                (54, o["side"]), (60, fix_time(t_cx)), (38, o["qty"])])
        t_er = t_cx + ms(rng.randint(8, 40))
        emit(t_er, "IN", "8", er_body(o, i, cx_id, exch_id, f"X{next(exec_counter)}", 4, 4, 0, 0, 0,
                                      t_er, orig=cid))
        row.update(ord_status="4", updated_ts=t_er)
    elif sc == "cancel_reject":
        t_cx = t + timedelta(seconds=rng.uniform(5, 60))
        emit(t_cx, "OUT", "F", [(41, cid), (11, cx_id), (37, exch_id), (1, o["client"]), (55, i.symbol),
                                (54, o["side"]), (60, fix_time(t_cx)), (38, o["qty"])])
        t_er = t_cx + ms(rng.randint(8, 40))
        emit(t_er, "IN", "9", [(37, exch_id), (11, cx_id), (41, cid), (39, 2), (102, 0), (434, 1),
                               (58, "Too late to cancel - order already filled")])
        row.update(updated_ts=t_er)
        sysline(t_er, "gw", "WARN", f"OrderCancelReject ClOrdID={cx_id} OrigClOrdID={cid} (too late to cancel)")

    if sc == "partial_mismatch":
        row["filled_qty"] = 7500      # <-- OMS thinks 7,500 filled; broker only reported 6,000
    return row


# --------------------------------------------------------------------------- #
# Session events and incidents (logon, heartbeats, drops, replays)
# --------------------------------------------------------------------------- #
def build_session_and_incidents(orphan, dup):
    # Start of day logon
    emit(at(6, 59, 56, 912), "OUT", "A", [(98, 0), (108, 30)])
    emit(at(6, 59, 57, 44), "IN", "A", [(98, 0), (108, 30)])
    sysline(at(6, 59, 50), "gw", "INFO", "Starting fixgateway 4.8.2 session=ACME_OMS->XBROKER version=FIX.4.2 HeartBtInt=30")
    sysline(at(6, 59, 52), "gw", "INFO", "Connecting to XBROKER 10.20.4.17:9876")
    sysline(at(6, 59, 56, 900), "gw", "INFO", "TCP connected, sending Logon MsgSeqNum=1")
    sysline(at(6, 59, 57, 60), "gw", "INFO", "Logon acknowledged by XBROKER, session ACTIVE")

    # NTP trouble that caused the first 35=3 (SendingTime accuracy)
    sysline(at(14, 21, 7), "chrony", None, "Can't synchronise: no selectable sources")
    sysline(at(14, 44, 3, 500), "chrony", None, "Selected source 10.20.0.5 (ntp1.corp.example)")
    sysline(at(14, 44, 3, 520), "chrony", None, "System clock wrong by 2.412417 seconds, adjustment started")

    # Pre-trade risk slowness -> latency spike cluster (acks in 15:02-15:09)
    sysline(at(15, 1, 48), "oms", "WARN", "pre-trade risk service slow: p99=2140ms threshold_ms=300")
    sysline(at(15, 1, 49), "oms", "WARN", "outbound order queue depth=412 high_water=250")
    sysline(at(15, 9, 31), "oms", "INFO", "pre-trade risk service latency back to normal: p99=41ms")
    sysline(at(15, 9, 32), "oms", "INFO", "outbound order queue drained depth=0")

    # ---- Incident A: silent TCP failure, drop, reconnect, resend/replay (15:39 - 15:42)
    for k, t in enumerate([at(15, 39, 12), at(15, 39, 42), at(15, 40, 12), at(15, 40, 42)]):
        emit(t + ms(30 + k * 7), "IN", "0", [])
    for t in [at(15, 39, 15), at(15, 39, 45), at(15, 40, 15), at(15, 40, 45), at(15, 41, 15)]:
        emit(t + ms(11), "OUT", "0", [])
    tr = emit(at(15, 41, 18), "OUT", "1", [(112, "TEST-154118")])
    sysline(at(15, 40, 44), "kern", None, "nf_conntrack: nf_conntrack: table full, dropping packet")
    sysline(at(15, 40, 51), "kern", None, "nf_conntrack: nf_conntrack: table full, dropping packet")
    sysline(at(15, 41, 3), "kern", None, "nf_conntrack: nf_conntrack: table full, dropping packet")
    sysline(at(15, 41, 18, 5), "gw", "WARN", lambda: f"No data from XBROKER for 36s (HeartBtInt=30), sending TestRequest 112=TEST-154118 MsgSeqNum={tr['seq']}")
    sysline(at(15, 41, 48), "gw", "ERROR", "TestRequest timeout: no Heartbeat within 30s, closing session")
    sysline(at(15, 41, 48, 100), "gw", "ERROR", "Session ACME_OMS->XBROKER DISCONNECTED reason=HEARTBEAT_TIMEOUT")
    sysline(at(15, 41, 53), "gw", "INFO", "Reconnect attempt 1/10 to 10.20.4.17:9876")
    sysline(at(15, 41, 53, 210), "gw", "ERROR", "connect() failed: Connection refused (ECONNREFUSED)")
    sysline(at(15, 42, 3), "gw", "INFO", "Reconnect attempt 2/10 to 10.20.4.17:9876")
    sysline(at(15, 42, 8), "gw", "ERROR", "connect() failed: Connection timed out (ETIMEDOUT)")
    sysline(at(15, 42, 8, 500), "gw", "INFO", "Reconnect attempt 3/10 to 10.20.4.17:9876")
    sysline(at(15, 42, 9), "gw", "INFO", "TCP connected, sending Logon")

    logon_out = emit(at(15, 42, 9, 12), "OUT", "A", [(98, 0), (108, 30)])
    emit(at(15, 42, 9, 108), "IN", "A", [(98, 0), (108, 30)])
    sysline(at(15, 42, 9, 110), "gw", "INFO", "Logon acknowledged by XBROKER, session ACTIVE")
    rr_in = emit(at(15, 42, 9, 150), "IN", "2", [(7, seq_of(orphan["d_event"])), (16, 0)])
    sysline(at(15, 42, 9, 155), "gw", "WARN", lambda: f"ResendRequest received BeginSeqNo={orphan['d_event']['seq']} EndSeqNo=0")
    # Classic bug: the OMS gap-fills over an application message that never left the box
    emit(at(15, 42, 9, 190), "OUT", "4", [(123, "Y"), (36, seq_of(logon_out))], replay_of=orphan["d_event"])
    sysline(at(15, 42, 9, 195), "gw", "INFO", lambda: f"Sent SequenceReset-GapFill NewSeqNo={logon_out['seq']} "
                                                       f"(replaced {logon_out['seq'] - orphan['d_event']['seq']} messages)")
    sysline(at(15, 42, 9, 300), "oms", "WARN", lambda: f"seq store checkpoint older than last processed inbound seq, requesting resend BeginSeqNo={dup['last_fill_event']['seq']}")
    emit(at(15, 42, 9, 400), "OUT", "2", [(7, seq_of(dup["last_fill_event"])), (16, 0)])
    emit(at(15, 42, 9, 520), "IN", "8", None, replay_of=dup["last_fill_event"])
    sysline(at(15, 42, 9, 525), "gw", "INFO", lambda: f"Received 1 replayed message with PossDupFlag=Y MsgSeqNum={dup['last_fill_event']['seq']}")
    sysline(at(15, 42, 9, 600), "oms", "INFO", "Session recovered, inbound queue resynced")
    for t in (at(15, 47), at(15, 52), at(15, 57)):
        sysline(t, "oms", "WARN", f"1 order(s) in PENDING_NEW for >{int((t - at(15, 41, 13)).total_seconds())}s ClOrdID={orphan['id']} "
                                  f"(sent 15:41:13) no ExecutionReport received")
    # Remember when the replay happened so the duplicate DB row can carry its timestamp
    dup["replay_ts"] = at(15, 42, 9, 520)

    # ---- Incident B: scheduled venue restart (clean logout/logon, no impact)
    emit(at(18, 12, 5, 100), "IN", "5", [(58, "Scheduled venue maintenance restart")])
    emit(at(18, 12, 5, 210), "OUT", "5", [])
    sysline(at(18, 12, 5, 120), "gw", "INFO", 'Logout received from XBROKER: "Scheduled venue maintenance restart"')
    sysline(at(18, 12, 5, 230), "gw", "WARN", "Session ACME_OMS->XBROKER DISCONNECTED reason=PEER_LOGOUT")
    sysline(at(18, 12, 12), "gw", "INFO", "Reconnect attempt 1/10 to 10.20.4.17:9876")
    sysline(at(18, 12, 12, 220), "gw", "ERROR", "connect() failed: Connection refused (ECONNREFUSED)")
    sysline(at(18, 12, 17), "gw", "INFO", "Reconnect attempt 2/10 to 10.20.4.17:9876")
    sysline(at(18, 12, 17, 900), "gw", "INFO", "TCP connected, sending Logon")
    emit(at(18, 12, 18, 3), "OUT", "A", [(98, 0), (108, 30)])
    emit(at(18, 12, 18, 140), "IN", "A", [(98, 0), (108, 30)])
    sysline(at(18, 12, 18, 150), "gw", "INFO", "Logon acknowledged by XBROKER, session ACTIVE")

    # End-of-day logout and a little realistic background noise
    emit(at(21, 5, 0, 20), "OUT", "5", [(58, "End of day")])
    emit(at(21, 5, 0, 131), "IN", "5", [(58, "End of day")])
    sysline(at(21, 5, 0, 140), "gw", "INFO", "Session ACME_OMS->XBROKER closed normally (end of day)")
    sysline(at(9, 15, 1), "cron", None, "(fixsvc) CMD (/opt/fixgw/bin/rotate_audit.sh)")
    sysline(at(16, 55, 12), "gw", "WARN", "Heartbeat round-trip 388ms threshold_ms=250")
    sysline(at(17, 30, 2), "gw", "WARN", "disk usage on /var/log at 81%")
    sysline(at(20, 0, 1), "cron", None, "(fixsvc) CMD (/opt/fixgw/bin/rotate_audit.sh)")


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE clients (
    client_id         TEXT PRIMARY KEY,
    client_name       TEXT NOT NULL,
    client_type       TEXT NOT NULL,
    country           TEXT NOT NULL,
    credit_limit_usd  REAL NOT NULL
);

-- WHY raw FIX codes (not friendly text): real OMS/ledger tables store what arrived on the wire,
-- and a support analyst has to know the codes. The ref_* tables below translate them when needed.
CREATE TABLE orders (
    -- NB: no FOREIGN KEY from ord_status/side/order_type to the ref_* tables on purpose. Codes
    -- come from the broker; if a venue starts sending a code we have not mapped, the row must
    -- still be stored. You find such rows with a LEFT JOIN (bonus exercise B3).
    order_id           TEXT PRIMARY KEY,           -- FIX ClOrdID (tag 11)
    client_id          TEXT NOT NULL REFERENCES clients(client_id),
    symbol             TEXT NOT NULL,
    security_desc      TEXT,
    asset_class        TEXT NOT NULL,              -- EQUITY | FX | DERIVATIVE
    side               TEXT NOT NULL,              -- 1=Buy 2=Sell
    order_type         TEXT NOT NULL,              -- 1=Market 2=Limit
    quantity           INTEGER NOT NULL,
    limit_price        REAL,
    filled_qty         INTEGER NOT NULL DEFAULT 0, -- OMS view of cumulative fills
    avg_px             REAL,
    ord_status         TEXT NOT NULL,              -- A,0,1,2,4,8
    currency           TEXT NOT NULL,
    multiplier         INTEGER NOT NULL DEFAULT 1, -- contract multiplier for notional
    venue              TEXT,
    exchange_order_id  TEXT,                       -- FIX OrderID (tag 37), NULL until acked
    reject_reason      TEXT,
    created_ts         TEXT NOT NULL,              -- UTC
    updated_ts         TEXT NOT NULL
);

-- Fills only (not acks/cancels/rejects). WHY NO unique constraint on exec_id: de-duplication
-- was left to the application, as it often is in legacy trade ledgers. If the database
-- enforced it, the replayed fill would have bounced and anomaly 3 could not exist. Lesson:
-- a missing constraint is a classic root cause - note it in your RCA recommendations.
CREATE TABLE executions (
    row_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_id    TEXT NOT NULL,                      -- FIX ExecID (tag 17)
    order_id   TEXT NOT NULL REFERENCES orders(order_id),
    exec_type  TEXT NOT NULL,                      -- 1=Partial fill 2=Fill
    last_qty   INTEGER NOT NULL,
    last_px    REAL NOT NULL,
    venue      TEXT,
    exec_ts    TEXT NOT NULL
);

-- WHY settlements is its own table: settlement is a separate post-trade process with its own
-- date (T+1/T+2), status and often its own system - and its own errors. Reconciling it
-- back to executions is the core of trade support (Exercise 4).
CREATE TABLE settlements (
    settlement_id  TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL REFERENCES orders(order_id),
    trade_date     TEXT NOT NULL,
    settle_date    TEXT NOT NULL,
    quantity       INTEGER NOT NULL,
    price          REAL NOT NULL,
    notional       REAL NOT NULL,                  -- quantity * price * multiplier, in currency
    currency       TEXT NOT NULL,
    settle_status  TEXT NOT NULL                   -- SETTLED | PENDING
);

-- Reference (static) data. In production most joins are transaction table -> static data
-- (instrument master, counterparty, status codes). Wrong or missing static data is itself a
-- common cause of breaks. These small tables let you practise the join (bonus exercises).
CREATE TABLE ref_ord_status (
    code         TEXT PRIMARY KEY,                 -- FIX tag 39 value
    name         TEXT NOT NULL,
    description  TEXT NOT NULL,
    is_terminal  INTEGER NOT NULL                  -- 1 = no further fills/changes expected
);
CREATE TABLE ref_side       (code TEXT PRIMARY KEY, name TEXT NOT NULL);   -- FIX tag 54
CREATE TABLE ref_order_type (code TEXT PRIMARY KEY, name TEXT NOT NULL);   -- FIX tag 40

CREATE INDEX ix_orders_client ON orders(client_id);
CREATE INDEX ix_exec_order    ON executions(order_id);
CREATE INDEX ix_settle_order  ON settlements(order_id);
"""


# Codes per FIX 4.2 tag 39 (OrdStatus): the subset a desk sees in practice. Terminal means the
# order is finished (filled, done for day, cancelled, replaced, rejected, expired).
REF_ORD_STATUS = [
    ("A", "PendingNew", "Sent to broker, no acknowledgement yet", 0),
    ("0", "New", "Acknowledged and working, no fills yet", 0),
    ("1", "PartiallyFilled", "Some quantity executed, remainder still working", 0),
    ("2", "Filled", "Fully executed", 1),
    ("3", "DoneForDay", "No more fills expected today", 1),
    ("4", "Canceled", "Cancelled before (completely) filled", 1),
    ("5", "Replaced", "Amended; superseded by a replacement order", 1),
    ("6", "PendingCancel", "Cancel requested, not yet confirmed", 0),
    ("8", "Rejected", "Refused by the broker/venue (risk, symbol, market closed...)", 1),
    ("C", "Expired", "Time in force elapsed without a full fill", 1),
    ("E", "PendingReplace", "Amend requested, not yet confirmed", 0),
]
REF_SIDE = [("1", "Buy"), ("2", "Sell")]
REF_ORDER_TYPE = [("1", "Market"), ("2", "Limit")]


def build_settlements(orders):
    # WHY only Filled orders: settlement happens once a trade is complete. Partially filled or
    # working orders have no settlement yet - so "Filled order with no settlement" is itself a
    # useful reconciliation check.
    rows = []
    for o in orders:
        r = o["row"]
        if r["ord_status"] != "2":
            continue
        i = o["inst"]
        # WHY average price: a settlement instruction is one line per order at the VWAP of its
        # fills, not one per fill - which is why duplicate fills do not change the settled price.
        price = r["avg_px"]
        if o["scenario"] == "price_discrepancy":
            price = 245.08                                   # executed 245.80, last two digits swapped
        notional = round(r["quantity"] * price * i.mult, 2)
        # Settlement cycle: US-style T+1 for equities/derivatives; FX spot is T+2 except USD/CAD
        # which settles T+1 (a genuine market convention).
        t_plus = 2 if (i.asset_class == "FX" and i.ccy != "CAD") else 1
        settle = TRADE_DATE + timedelta(days=t_plus)
        rows.append(dict(order_id=r["order_id"], trade_date=TRADE_DATE.strftime("%Y-%m-%d"),
                         settle_date=settle.strftime("%Y-%m-%d"), quantity=r["quantity"], price=price,
                         notional=notional, currency=r["currency"],
                         settle_status="SETTLED" if settle <= AS_OF else "PENDING"))
    for n, row in enumerate(rows, 1):
        row["settlement_id"] = f"STL-{n:05d}"
    return rows


def write_db(orders, settlements):
    DB_PATH.parent.mkdir(exist_ok=True)
    DB_PATH.unlink(missing_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO clients VALUES (?,?,?,?,?)", [c[:5] for c in CLIENTS])
    con.executemany("INSERT INTO ref_ord_status VALUES (?,?,?,?)", REF_ORD_STATUS)
    con.executemany("INSERT INTO ref_side VALUES (?,?)", REF_SIDE)
    con.executemany("INSERT INTO ref_order_type VALUES (?,?)", REF_ORDER_TYPE)

    def insert(table, rows, cols):
        con.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [[r[c] for c in cols] for r in rows])

    ordrows = [o["row"] for o in orders]
    for r in ordrows:
        r["created_ts"], r["updated_ts"] = iso(r["created_ts"]), iso(r["updated_ts"])
    insert("orders", ordrows, ["order_id", "client_id", "symbol", "security_desc", "asset_class", "side",
                               "order_type", "quantity", "limit_price", "filled_qty", "avg_px", "ord_status",
                               "currency", "multiplier", "venue", "exchange_order_id", "reject_reason",
                               "created_ts", "updated_ts"])
    execs = sorted(executions, key=lambda e: e["exec_ts"])
    for e in execs:
        e["exec_ts"] = iso(e["exec_ts"])
    insert("executions", execs, ["exec_id", "order_id", "exec_type", "last_qty", "last_px", "venue", "exec_ts"])
    insert("settlements", settlements, ["settlement_id", "order_id", "trade_date", "settle_date", "quantity",
                                        "price", "notional", "currency", "settle_status"])
    con.commit()
    return con


def write_logs():
    FIX_LOG.parent.mkdir(exist_ok=True)
    with open(FIX_LOG, "w", encoding="utf-8", newline="\n") as f:
        for ev in events:
            f.write(f"{iso(ev['ts'])} [{ev['dir']:<3}] {render(ev)}\n")
    lines = sorted(sys_lines, key=lambda x: x[0])
    with open(SYS_LOG, "w", encoding="utf-8", newline="\n") as f:
        for ts, proc, level, msg in lines:
            f.write(render_sysline(ts, proc, level, msg) + "\n")
    return len(events), len(lines)


# --------------------------------------------------------------------------- #
# Self-check: prove each anomaly exists exactly as designed
# --------------------------------------------------------------------------- #
# WHY a self-check: a data generator that silently produces the wrong anomaly wastes your
# practice time. Each entry is "SQL that should find exactly N rows". Check 2 expects 2, not 1,
# because the duplicate-ExecID order ALSO has executions > filled_qty - an honest side effect.
CHECKS = {
    "1. orphaned order (status A, no exec)": (
        """SELECT o.order_id FROM orders o LEFT JOIN executions e ON e.order_id = o.order_id
           WHERE o.ord_status = 'A' AND o.exchange_order_id IS NULL AND e.order_id IS NULL""", 1),
    "2. executed qty != filled_qty": (
        """SELECT o.order_id FROM orders o JOIN executions e ON e.order_id = o.order_id
           GROUP BY o.order_id HAVING SUM(e.last_qty) <> o.filled_qty""", 2),   # incl. the duplicate-exec order
    "3. duplicate exec_id": (
        "SELECT exec_id FROM executions GROUP BY exec_id HAVING COUNT(*) > 1", 1),
    "4. settlement price/notional break": (
        """SELECT s.order_id FROM settlements s JOIN orders o ON o.order_id = s.order_id
           WHERE ABS(s.price - o.avg_px) > 0.000001
              OR ABS(s.notional - o.filled_qty * o.avg_px * o.multiplier) > 0.01""", 1),
    "5. rejected, credit limit (35=8, 39=8)": (
        """SELECT o.order_id FROM orders o JOIN clients c ON c.client_id = o.client_id
           WHERE o.ord_status = '8' AND o.quantity * o.limit_price * o.multiplier > c.credit_limit_usd""", 1),
    "6. every order status code has reference data": (
        """SELECT DISTINCT o.ord_status FROM orders o
           LEFT JOIN ref_ord_status r ON r.code = o.ord_status WHERE r.code IS NULL""", 0),
}


def verify(con, show_ids):
    ok = True
    print("\nAnomaly self-check:")
    for name, (sql, expected) in CHECKS.items():
        found = [r[0] for r in con.execute(sql)]
        good = len(found) == expected
        ok &= good
        print(f"  [{'OK' if good else 'FAIL'}] {name}: {len(found)} found (expected {expected})"
              + (f"  -> {', '.join(found)}" if show_ids else ""))
    return ok


def main():
    parser = argparse.ArgumentParser(description="Seed the Trade Support Sandbox (DB + logs).")
    parser.add_argument("--answer-key", action="store_true", help="print the order/exec IDs of the injected anomalies")
    args = parser.parse_args()

    # WHY sort before numbering: a real OMS issues ClOrdIDs in the order orders are created, so
    # ORD-100001 is the earliest of the day. It also means anomaly IDs are not a giveaway.
    specs = anomaly_specs() + normal_specs()
    specs.sort(key=lambda s: s["ts"])
    for n, s in enumerate(specs, 100001):
        s["id"] = f"ORD-{n}"
    orders = specs
    for o in orders:
        build_flow(o)
        executions.extend(o["fills"])
    orphan = next(o for o in orders if o["scenario"] == "orphan")
    dup = next(o for o in orders if o["scenario"] == "dup_exec")
    build_session_and_incidents(orphan, dup)

    # Duplicate booking: the replayed ExecutionReport is booked a second time.
    # WHY a deep copy of the same exec_id/qty/px with a later timestamp: that is exactly what a
    # booking process without de-duplication produces - an identical row booked at replay time.
    dup_fill = dict(dup["fills"][0])
    dup_fill["exec_ts"] = dup["replay_ts"]
    executions.append(dup_fill)
    dup["row"]["updated_ts"] = max(dup["row"]["updated_ts"], dup["replay_ts"])

    # Sort by time, THEN number: see emit(). Stable sort keeps same-timestamp messages in emit order.
    events.sort(key=lambda e: e["ts"])
    assign_sequence_numbers()
    settlements = build_settlements(orders)
    con = write_db(orders, settlements)
    n_fix, n_sys = write_logs()

    print(f"Database : {DB_PATH.relative_to(ROOT)}")
    for t in ("clients", "orders", "executions", "settlements", "ref_ord_status"):
        print(f"  {t:<12} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>4} rows")
    print(f"FIX log  : {FIX_LOG.relative_to(ROOT)}  ({n_fix} messages)")
    print(f"Sys log  : {SYS_LOG.relative_to(ROOT)}  ({n_sys} lines)")
    ok = verify(con, args.answer_key)
    con.close()
    if not ok:
        raise SystemExit("Self-check failed")
    print("\nDone - 0 errors.")


if __name__ == "__main__":
    main()
