#!/usr/bin/env python3
"""
fix_parser.py - turn raw FIX 4.2 messages into human-readable tag/value pairs.

A FIX message is a flat string of  tag=value  fields separated by the SOH
character (ASCII 0x01). Logs usually show SOH as '|' (or ^A), which is what
this tool accepts:

    8=FIX.4.2|9=216|35=8|...|39=2|150=2|54=1|55=MSFT|...|10=184|

Usage:
    python tools/fix_parser.py "8=FIX.4.2|9=...|35=8|39=2|54=1|55=AAPL|...|10=123|"
    python tools/fix_parser.py logs/fix_gateway.log                       # whole log file
    python tools/fix_parser.py logs/fix_gateway.log --filter 11=ORD-100020
    python tools/fix_parser.py logs/fix_gateway.log --filter 35=8 --filter 39=8
    python tools/fix_parser.py logs/fix_gateway.log --compact             # one line per message
    Get-Content msg.txt | python tools/fix_parser.py -                    # read stdin

Every message is also checked against its own BodyLength (tag 9) and CheckSum
(tag 10); mismatches usually mean a message was hand-edited, truncated, or
mangled in transit.
"""

import argparse
import os
import re
import sys

SOH = "\x01"

# --------------------------------------------------------------------------- #
# FIX 4.2 data dictionary (only the tags this sandbox uses, plus common extras)
# --------------------------------------------------------------------------- #
TAG_NAMES = {
    1: "Account", 6: "AvgPx", 7: "BeginSeqNo", 8: "BeginString", 9: "BodyLength", 10: "CheckSum",
    11: "ClOrdID", 14: "CumQty", 15: "Currency", 16: "EndSeqNo", 17: "ExecID", 20: "ExecTransType",
    21: "HandlInst", 30: "LastMkt", 31: "LastPx", 32: "LastShares", 34: "MsgSeqNum", 35: "MsgType",
    36: "NewSeqNo", 37: "OrderID", 38: "OrderQty", 39: "OrdStatus", 40: "OrdType", 41: "OrigClOrdID",
    43: "PossDupFlag", 44: "Price", 45: "RefSeqNum", 49: "SenderCompID", 52: "SendingTime",
    54: "Side", 55: "Symbol", 56: "TargetCompID", 58: "Text", 59: "TimeInForce", 60: "TransactTime",
    98: "EncryptMethod", 100: "ExDestination", 102: "CxlRejReason", 103: "OrdRejReason",
    107: "SecurityDesc", 108: "HeartBtInt", 112: "TestReqID", 122: "OrigSendingTime",
    123: "GapFillFlag", 141: "ResetSeqNumFlag", 150: "ExecType", 151: "LeavesQty",
    167: "SecurityType", 200: "MaturityMonthYear", 201: "PutOrCall", 202: "StrikePrice",
    371: "RefTagID", 372: "RefMsgType", 373: "SessionRejectReason", 434: "CxlRejResponseTo",
}

# Enumerated values: tag -> {raw value: meaning}
ENUMS = {
    35: {"0": "Heartbeat", "1": "TestRequest", "2": "ResendRequest", "3": "Reject",
         "4": "SequenceReset", "5": "Logout", "8": "ExecutionReport", "9": "OrderCancelReject",
         "A": "Logon", "D": "NewOrderSingle", "F": "OrderCancelRequest",
         "G": "OrderCancelReplaceRequest", "H": "OrderStatusRequest"},
    39: {"0": "New", "1": "PartiallyFilled", "2": "Filled", "3": "DoneForDay", "4": "Canceled",
         "5": "Replaced", "6": "PendingCancel", "7": "Stopped", "8": "Rejected", "9": "Suspended",
         "A": "PendingNew", "B": "Calculated", "C": "Expired", "E": "PendingReplace"},
    # FIX 4.2 reports fills as 1/2. 'F' (Trade) only exists from FIX 4.4, but some venues
    # send it on 4.2 sessions, so we translate it too.
    150: {"0": "New", "1": "PartialFill", "2": "Fill", "3": "DoneForDay", "4": "Canceled",
          "5": "Replace", "6": "PendingCancel", "7": "Stopped", "8": "Rejected", "9": "Suspended",
          "A": "PendingNew", "B": "Calculated", "C": "Expired", "D": "Restated",
          "E": "PendingReplace", "F": "Trade (FIX 4.4+)", "G": "TradeCorrect (FIX 4.4+)",
          "H": "TradeCancel (FIX 4.4+)", "I": "OrderStatus (FIX 4.4+)"},
    54: {"1": "Buy", "2": "Sell", "3": "BuyMinus", "4": "SellPlus", "5": "SellShort",
         "6": "SellShortExempt"},
    40: {"1": "Market", "2": "Limit", "3": "Stop", "4": "StopLimit"},
    59: {"0": "Day", "1": "GoodTillCancel", "2": "AtTheOpening", "3": "ImmediateOrCancel",
         "4": "FillOrKill", "6": "GoodTillDate"},
    20: {"0": "New", "1": "Cancel", "2": "Correct", "3": "Status"},
    21: {"1": "AutomatedNoIntervention", "2": "AutomatedWithIntervention", "3": "Manual"},
    43: {"Y": "PossibleDuplicate", "N": "OriginalTransmission"},
    123: {"Y": "GapFill", "N": "SequenceReset-Reset"},
    141: {"Y": "ResetSeqNums", "N": "KeepSeqNums"},
    98: {"0": "None"},
    167: {"CS": "CommonStock", "PS": "PreferredStock", "FOR": "ForeignExchangeContract",
          "FUT": "Future", "OPT": "Option"},
    201: {"0": "Put", "1": "Call"},
    102: {"0": "TooLateToCancel", "1": "UnknownOrder", "2": "BrokerOption",
          "3": "AlreadyPendingCancelOrReplace"},
    103: {"0": "BrokerOption", "1": "UnknownSymbol", "2": "ExchangeClosed",
          "3": "OrderExceedsLimit", "4": "TooLateToEnter", "5": "UnknownOrder",
          "6": "DuplicateOrder", "7": "DuplicateOfVerballyCommunicatedOrder", "8": "StaleOrder"},
    434: {"1": "OrderCancelRequest", "2": "OrderCancelReplaceRequest"},
    373: {"0": "InvalidTagNumber", "1": "RequiredTagMissing", "2": "TagNotDefinedForThisMessageType",
          "3": "UndefinedTag", "4": "TagSpecifiedWithoutValue", "5": "ValueIncorrectForTag",
          "6": "IncorrectDataFormat", "9": "CompIDProblem", "10": "SendingTimeAccuracyProblem",
          "11": "InvalidMsgType"},
}


def meaning(tag, value):
    """Return the human meaning of an enumerated value, e.g. (39, '2') -> 'Filled'."""
    return ENUMS.get(tag, {}).get(value)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
FIX_START = re.compile(r"8=FIX\.\d\.\d|8=FIXT\.\d\.\d")


def normalise(text):
    """Return the message with a real SOH delimiter, whatever the input used ('|', ^A or SOH)."""
    text = text.replace("^A", SOH)
    if SOH not in text:
        text = text.replace("|", SOH)
    return text


def split_fields(norm):
    """'35=8\\x0139=2\\x01' -> [(35, '8'), (39, '2')]. Non-numeric tags are kept as text."""
    fields = []
    for chunk in norm.split(SOH):
        if not chunk:
            continue
        tag, _, value = chunk.partition("=")
        fields.append((int(tag) if tag.isdigit() else tag, value))
    return fields


def validate(norm):
    """Check BodyLength (9) and CheckSum (10). Returns a list of problems (empty = healthy)."""
    problems = []
    pos10 = norm.rfind(SOH + "10=")
    pos9 = norm.find(SOH + "9=")
    if pos10 == -1:
        return ["CheckSum (10) missing - message truncated?"]
    # CheckSum = sum of every byte before the '10=' field, modulo 256
    expected_cs = sum(norm[:pos10 + 1].encode()) % 256
    got_cs = norm[pos10 + 4:].split(SOH)[0]
    if not got_cs.isdigit() or int(got_cs) != expected_cs:
        problems.append(f"CheckSum mismatch: message says {got_cs}, calculated {expected_cs:03d}")
    # BodyLength = bytes from after the '9=' field up to and including the SOH before '10='
    if pos9 == -1:
        problems.append("BodyLength (9) missing")
    else:
        body_start = norm.find(SOH, pos9 + 1) + 1
        expected_len = pos10 + 1 - body_start
        got_len = norm[pos9 + 3:body_start - 1]
        if not got_len.isdigit() or int(got_len) != expected_len:
            problems.append(f"BodyLength mismatch: message says {got_len}, calculated {expected_len}")
    return problems


def extract_messages(lines):
    """Yield (line_number, log_prefix, normalised_message) for every line holding a FIX message."""
    for n, line in enumerate(lines, 1):
        m = FIX_START.search(line)
        if not m:
            continue      # blank line, comment, or non-FIX log noise
        yield n, line[:m.start()].strip(), normalise(line[m.start():].rstrip("\r\n"))


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def matches(fields, filters):
    """True if the message contains every requested TAG=VALUE pair."""
    return all(any(str(t) == tag and v == val for t, v in fields) for tag, val in filters)


def summarise(fields):
    """One-line plain-English summary of a message, for --compact and the message header."""
    d = {}
    for t, v in fields:
        d.setdefault(t, v)                                  # first occurrence wins
    mt = d.get(35, "?")
    side, sym = meaning(54, d.get(54, "")) or d.get(54, ""), d.get(55, "")
    if mt == "D":
        price = f" @ {d[44]}" if 44 in d else ""
        return f"{d.get(11, '')} {side.upper()} {d.get(38, '')} {sym} {meaning(40, d.get(40, '')) or ''}{price}"
    if mt == "8":
        parts = [d.get(11, ""), f"exec={meaning(150, d.get(150, '')) or d.get(150, '')}",
                 f"status={meaning(39, d.get(39, '')) or d.get(39, '')}", side.upper(), sym]
        if 32 in d:
            parts.append(f"last={d[32]}@{d.get(31, '?')}")
        parts.append(f"cum={d.get(14, '?')}/{d.get(38, '?')}")
        if 17 in d:
            parts.append(f"execid={d[17]}")
        if d.get(43) == "Y":
            parts.append("**POSSDUP**")
        if 58 in d:
            parts.append(f'"{d[58]}"')
        return " ".join(p for p in parts if p)
    if mt == "F":
        return f"cancel {d.get(41, '')} as {d.get(11, '')} ({sym})"
    if mt == "9":
        return f"cancel rejected for {d.get(41, '')}: {meaning(102, d.get(102, '')) or ''} - {d.get(58, '')}"
    if mt == "3":
        return (f"session reject of seq {d.get(45, '?')} (35={d.get(372, '?')}, tag {d.get(371, '?')}): "
                f"{meaning(373, d.get(373, '')) or d.get(373, '')}")
    if mt == "2":
        return f"resend request seq {d.get(7, '?')} to {d.get(16, '?')}"
    if mt == "4":
        return f"sequence reset ({'gap fill' if d.get(123) == 'Y' else 'reset'}) -> next seq {d.get(36, '?')}"
    return d.get(58, "")


def print_full(index, line_no, prefix, norm, fields):
    mt = next((v for t, v in fields if t == 35), "?")
    name = meaning(35, mt) or "UnknownMsgType"
    where = f"line {line_no}" if line_no else "input"
    print("=" * 78)
    print(f"Message {index}  [{where}]  {prefix}".rstrip())
    print(f"{name} (35={mt}): {summarise(fields)}")
    print("-" * 78)
    for tag, value in fields:
        label = TAG_NAMES.get(tag, "(unknown tag)") if isinstance(tag, int) else "(malformed)"
        note = meaning(tag, value) if isinstance(tag, int) else None
        print(f"  {str(tag):<5} {label:<20} {value}" + (f"  ->  {note}" if note else ""))
    problems = validate(norm)
    print("  [integrity] " + ("BodyLength and CheckSum OK" if not problems else "; ".join(problems)))


def print_compact(line_no, prefix, fields):
    mt = next((v for t, v in fields if t == 35), "?")
    print(f"{line_no:>4}  {prefix:<29} {meaning(35, mt) or mt:<18} {summarise(fields)}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Decode raw FIX 4.2 messages (from a string, a log file, or stdin) into tag/value pairs.")
    ap.add_argument("source", help="raw FIX message, path to a log file, or '-' to read stdin")
    ap.add_argument("-f", "--filter", action="append", default=[], metavar="TAG=VALUE",
                    help="only show messages containing this pair, e.g. 11=ORD-100020 or 35=8 (repeatable, AND)")
    ap.add_argument("-c", "--compact", action="store_true", help="one summary line per message")
    args = ap.parse_args()

    filters = []
    for f in args.filter:
        tag, sep, val = f.partition("=")
        if not sep or not tag.isdigit():
            ap.error(f"bad --filter '{f}' (expected TAG=VALUE, e.g. 39=8)")
        filters.append((tag, val))

    if args.source == "-":
        lines = sys.stdin.read().splitlines()
        is_file = True
    elif os.path.isfile(args.source):
        with open(args.source, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        is_file = True
    elif FIX_START.search(args.source):
        lines, is_file = [args.source], False
    else:
        sys.exit(f"error: '{args.source}' is neither a file nor a FIX message (should contain 8=FIX.4.x)")

    shown = total = 0
    for line_no, prefix, norm in extract_messages(lines):
        total += 1
        fields = split_fields(norm)
        if not matches(fields, filters):
            continue
        shown += 1
        if args.compact:
            print_compact(line_no, prefix, fields)
        else:
            print_full(shown, line_no if is_file else None, prefix, norm, fields)
    if total == 0:
        sys.exit("error: no FIX messages found in input")
    if is_file:
        print(f"\n{shown} of {total} messages shown.", file=sys.stderr)


if __name__ == "__main__":
    main()
