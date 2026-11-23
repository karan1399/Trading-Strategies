"""Deterministic checks for evidence captured by capture_round_evidence.py."""

from datetime import datetime, timedelta, timezone
import json
import math
import operator
import re


COMPARISONS = {"gte": operator.ge, "gt": operator.gt, "lte": operator.le,
               "lt": operator.lt, "eq": operator.eq}
FUNNEL_FEEDS = {
    "FUNNEL-01": {("liqlevels", "3d"), ("liqlevels", "7d"), ("liqlevels", "30d"), ("stopmap", None)},
    "FUNNEL-02": {("signals", None)},
    "FUNNEL-03": {("bookwalls", None), ("lto", None)},
    "FUNNEL-04": {("market", None), ("narrative", None)},
    "FUNNEL-05": {("market", None), ("signals", None)},
}
FX = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF", "USDINR"}


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("expected UTC timestamp")
    return parsed


def feed_symbols(instrument):
    native = instrument.upper().split(".", 1)[0]
    symbols = {native}
    if native not in FX:
        base = re.sub(r"(?:USDT|USD)$", "", native)
        symbols.add(base)
        if base == "XAUT":
            symbols.add("XAU")
    return symbols


def audit_source(source, capture, payload, wake, cutoff, allowed, instrument, window):
    keys = ("round_id", "wake_id", "spec_sha256")
    if (not wake.get("wake_id") or any(capture.get(k) != wake.get(k) for k in keys)
            or any(capture.get(k) != source.get(k) for k in ("path", "sha256"))):
        raise ValueError("missing capture receipt for this wake and payload")
    if capture.get("feed") not in allowed:
        raise ValueError("capture endpoint cannot support this evidence row")
    if not isinstance(payload, dict):
        raise ValueError("captured response is not an object")
    capture_start = timestamp(capture["requested_utc"])
    capture_end = timestamp(capture["received_utc"])
    if cutoff is None or not timestamp(wake["observed_utc"]) <= capture_start <= capture_end <= cutoff:
        raise ValueError("capture is outside this round's acquisition window")
    if instrument is None:
        return payload, capture, capture_end, capture_end

    query = capture.get("query", {})
    asset = payload.get("asset", {})
    sym = payload.get("sym") or (asset.get("sym") if isinstance(asset, dict) else None)
    if not isinstance(sym, str) or sym != query.get("sym") or sym.upper() not in feed_symbols(instrument):
        raise ValueError("actual captured symbol does not match this instrument")
    if (payload.get("ok") is not True or payload.get("supported") is False
            or payload.get("stale") is True or payload.get("partial") is True
            or (isinstance(asset, dict) and asset.get("supported") is False)):
        raise ValueError("captured feed is failed, unsupported, stale or partial")
    if window is not None and (query.get("range") != window or payload.get("range") != window):
        raise ValueError("captured liquidation range does not match this window")
    ms = payload.get("dataAt") or payload.get("updatedAt")
    if type(ms) not in (int, float) or not math.isfinite(ms):
        raise ValueError("provider timestamp unavailable")
    provider_at = datetime.fromtimestamp(ms / 1000, timezone.utc)
    if provider_at > capture_end:
        raise ValueError("provider timestamp is newer than capture")
    return payload, capture, capture_end, provider_at


def check_inventory(venue, body):
    if venue == "delta":
        pages = body["pages"]
        if not isinstance(pages, list) or not pages or pages[-1].get("meta", {}).get("after"):
            raise ValueError("Delta inventory pagination is incomplete")
        if not all(page.get("success") is True and page.get("meta", {}).get("after") for page in pages[:-1]):
            raise ValueError("Delta inventory page or cursor missing")
        if pages[-1].get("success") is not True:
            raise ValueError("Delta inventory request failed")
        items = [(str(item["id"]), item["symbol"]) for page in pages for item in page["result"]]
        return inventory_map(items)
    if body.get("login") != 5126976:
        raise ValueError("MT5 inventory account mismatch")
    return inventory_map([(item["name"], item["name"]) for item in body["symbols"] if item["trade_mode"] in (1, 2, 4)])


def inventory_map(items):
    mapping = dict(items)
    if not items or len(mapping) != len(items):
        raise ValueError("venue inventory is empty or has duplicate IDs")
    return mapping


def book_instrument(payload, venue, key, symbol):
    """Require a broker book record for this exact instrument."""
    if isinstance(payload, dict) and "response" in payload:
        response = payload["response"]
        if response.get("isError") or payload.get("tool") not in {"get_margined_positions", "get_open_orders"}:
            raise ValueError("expected a successful broker orders or positions response")
        payload = json.loads(response["content"][0]["text"])

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict) and isinstance(payload.get("result"), list):
        records = payload["result"]
    elif isinstance(payload, dict) and any(name in payload for name in ("positions", "orders")):
        records = payload.get("positions", []) + payload.get("orders", [])
    else:
        raise ValueError("expected broker orders or positions, not an inventory or market payload")

    for record in records:
        if not isinstance(record, dict):
            continue
        if venue == "delta":
            order = record.get("id") is not None and record.get("state") in {"open", "pending"}
            position = record.get("entry_price") is not None
            if (str(record.get("product_id")) == key and record.get("product_symbol") == symbol
                    and record.get("size") is not None and (order or position)):
                return
        elif (venue == "mt5" and record.get("symbol") == key and key == symbol
              and type(record.get("ticket")) is int and record["ticket"] > 0):
            return
    raise ValueError("instrument ID and venue symbol missing from the cited broker book")


def direct_value(body, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("a JSON pointer to the observed value is required")
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(body, list):
            if not token.isdecimal() or int(token) >= len(body):
                raise ValueError("evidence pointer does not resolve to an array element")
            body = body[int(token)]
        elif isinstance(body, dict) and token in body:
            body = body[token]
        else:
            raise ValueError("evidence pointer does not resolve to a field")
    return body


def liquidation_pace(body, received):
    end = received.timestamp() * 1000
    start = end - 300_000
    venues = body["venues"]
    events = body["asset"]["events"]
    if (not venues or not all(v["status"] == "open" and v.get("connectedAt") is not None
                               and v["connectedAt"] <= start and (v.get("lastFrameAt") or 0) >= end - 60_000
                               for v in venues)):
        raise ValueError("liquidation feeds do not show continuous five-minute coverage")
    if len(events) >= 400 and min(row["t"] for row in events) > start:
        raise ValueError("capped liquidation tape does not cover five minutes")
    deduplicated = {}
    for row in events:
        if row.get("asset") != body["asset"]["sym"]:
            raise ValueError("liquidation tape contains a different asset")
        if start < row["t"] <= end:
            key = tuple(row[field] for field in ("venue", "pair", "t", "side", "price", "qtyBase"))
            usd = row["usd"]
            if type(usd) not in (int, float) or not math.isfinite(usd) or usd < 0:
                raise ValueError("liquidation event USD is invalid")
            deduplicated[key] = usd
    return math.fsum(deduplicated.values())


def screen_observations(row, funnel, instrument, label, source, issues):
    required = FUNNEL_FEEDS.get(funnel)
    if required is None:
        return
    observations = row.get("checks")
    if not isinstance(observations, list) or not observations:
        issues.append(f"{label}: missing measured funnel inputs")
        return
    feeds = set()
    triggers = []
    for check in observations:
        try:
            feed = check["feed"]
            window = check.get("range")
            if (feed, window) not in required:
                raise ValueError("check is not a source for this funnel")
            captured = source(check["source"], label, {feed}, instrument, window)
            if captured is None:
                continue
            body, _, received, observed = captured
            if (received - observed).total_seconds() > 300:
                raise ValueError("funnel evidence is more than five minutes old at capture")
            value = direct_value(body, check["pointer"])
            if value is None or value != check["observed"]:
                raise ValueError("funnel input differs from the captured field")
            operation, threshold = check["op"], check["threshold"]
            if operation != "eq" and not all(type(number) in (int, float) and math.isfinite(number)
                                               for number in (value, threshold)):
                raise ValueError("ordered comparisons require finite numeric values")
            triggers.append(COMPARISONS[operation](value, threshold))
            feeds.add((feed, window))
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as exc:
            issues.append(f"{label}: {exc}")
    if feeds != required or not triggers:
        issues.append(f"{label}: required funnel source coverage is incomplete")
    elif row.get("status") != ("HIT" if any(triggers) else "NO_HIT"):
        issues.append(f"{label}: disposition contradicts measured inputs")
