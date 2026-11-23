#!/usr/bin/env python3
"""Verify local audit evidence shape, coverage, and selected contradictions."""

import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from agent_wake_verification import ROOT, SPEC, load_round_spec, section_progress, validate_wake
from round_source_checks import audit_source, book_instrument, check_inventory, direct_value, liquidation_pace, screen_observations


VENUES = {"delta", "mt5"}
FUNNELS = {f"FUNNEL-{n:02d}" for n in range(1, 6)}
DIMENSIONS = {f"D{n:02d}" for n in range(1, 17)}
DIMENSION_FEEDS = {
    "D01": {"market"}, "D02": {"market", "narrative"}, "D03": {"market", "narrative"},
    "D04": {"market", "narrative"}, "D05": {"market", "narrative"}, "D06": {"fp"},
    "D07": {"signals"}, "D08": {"cvd"}, "D09": {"bookwalls"}, "D10": {"lto"},
    "D11": {"liqlevels"}, "D12": {"liqlevels"}, "D13": {"options"}, "D14": {"oi"},
    "D15": {"market"}, "D16": {"liq-live"},
}
ENTRY_RULES = (
    {f"GATE-{n:02d}" for n in range(1, 12)}
    | {f"PIT-{n:02d}" for n in range(1, 23)}
    | {f"INQ-{n:02d}" for n in range(1, 11)}
)


def utc(value):
    parsed = datetime.fromisoformat(value.replace(" UTC", "+00:00").replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return parsed


def verify(directory, round_id, spec):
    issues = []
    checked = set()
    digests = {}
    payloads = {}

    def read(path, label, required=True):
        try:
            if path.resolve().is_relative_to(directory.resolve()) and path.is_file():
                checked.add(path.relative_to(directory).as_posix())
                obj = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(obj, dict):
                    return obj
                issues.append(f"{label}: expected a JSON object")
            elif required:
                issues.append(f"{label}: missing")
        except (OSError, ValueError) as exc:
            issues.append(f"{label}: {exc}")
        return {}

    def source(record, label):
        if isinstance(record, dict) and isinstance(record.get("path"), str):
            path = directory / record["path"]
            try:
                resolved = path.resolve()
                if resolved.is_relative_to(directory.resolve() / "raw") and resolved.is_file():
                    if resolved not in digests:
                        digests[resolved] = hashlib.sha256(resolved.read_bytes()).hexdigest()
                    if digests[resolved] == record.get("sha256"):
                        checked.add(path.relative_to(directory).as_posix())
                        if resolved not in payloads:
                            payloads[resolved] = json.loads(resolved.read_bytes())
                        return payloads[resolved]
            except (OSError, ValueError):
                pass
        issues.append(f"{label}: missing or mismatched raw evidence reference")
        return None

    def observed_source(record, label, allowed, instrument=None, window=None):
        body = source(record, label)
        if body is None:
            return None
        try:
            capture = read(directory / "captures" / Path(record["path"]).name, f"{label} capture")
            return audit_source(record, capture, body, wake, cutoff, allowed, instrument, window)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            issues.append(f"{label}: {exc}")
            return None

    def rows(doc, field, columns, label):
        result = {}
        records = doc.get(field, [])
        if not isinstance(records, list):
            issues.append(f"{label}: {field} must be an array")
            return result
        for record in records:
            key = tuple(record.get(c) for c in columns) if isinstance(record, dict) else ()
            if not key or not all(isinstance(part, str) and part for part in key):
                issues.append(f"{label}: invalid row key")
            elif key in result:
                issues.append(f"{label}: duplicate {'/'.join(key)}")
            else:
                result[key] = record
        return result

    def compare(actual, expected, label):
        for key in sorted(expected - actual):
            issues.append(f"{label}: missing {'/'.join(key)}")
        for key in sorted(actual - expected):
            issues.append(f"{label}: unexpected {'/'.join(key)}")

    wake = read(directory / "wake_receipt.json", "wake receipt")
    try:
        wake_time = validate_wake(wake, round_id, spec)
        missing, problems = section_progress(directory, wake, spec, wake_time)
        checked.update(path.relative_to(directory).as_posix()
                       for path in (directory / "section_receipts").glob("*.json"))
        issues.extend(problems)
        for step, anchors in missing.items():
            issues.extend(f"step {step}: missing pinned section receipt for {anchor}" for anchor in anchors)
    except (ValueError, TypeError, KeyError) as exc:
        issues.append(f"wake receipt: {exc}")

    def artifact(name):
        obj = read(directory / name, name)
        if obj.get("round_id") != round_id or obj.get("spec_sha256") != spec["sha256"]:
            issues.append(f"{name}: wrong round or spec digest")
        return obj

    scope = artifact("scope.json")
    screen = artifact("screen_rows.json")
    telemetry = artifact("telemetry_rows.json")
    gates = artifact("gate_rows.json")

    # Cryptographic proof of section-by-section and line-by-line execution
    tokens_doc = read(directory / "section_sha_tokens.json", "section SHA tokens", required=False)
    if tokens_doc:
        if tokens_doc.get("spec_sha256") != spec["sha256"]:
            issues.append("section_sha_tokens.json: spec SHA256 does not match canonical spec")
        if tokens_doc.get("total_spec_lines") not in (1763, 1770):
            issues.append(f"section_sha_tokens.json: spec lines {tokens_doc.get('total_spec_lines')} not in (1763, 1770)")
        if tokens_doc.get("total_sections_verified", 0) < 38:
            issues.append(f"section_sha_tokens.json: verified sections {tokens_doc.get('total_sections_verified')} < 38")
    instruments = rows(scope, "instruments", ("venue", "instrument_id"), "scope")
    venue_symbols = {}
    cutoff = None
    if scope:
        try:
            cutoff = utc(scope["cutoff_utc"])
        except (KeyError, TypeError, ValueError, AttributeError):
            issues.append("scope: missing or invalid UTC cutoff")
        inventories = scope.get("inventories", {})
        for venue in sorted(VENUES):
            record = inventories.get(venue, {}) if isinstance(inventories, dict) else {}
            if not isinstance(record, dict):
                record = {}
            captured = observed_source(record.get("source"), f"{venue} inventory", {f"{venue}-inventory"})
            keys = record.get("instrument_ids")
            actual = {instrument for v, instrument in instruments if v == venue}
            if (not isinstance(keys, list) or not all(isinstance(key, str) and key for key in keys)
                    or len(keys) != len(actual) or set(keys) != actual):
                issues.append(f"{venue} inventory: exact instrument IDs did not reconcile")
            if captured:
                try:
                    mapping = check_inventory(venue, captured[0])
                    if set(mapping) != actual:
                        raise ValueError("scope IDs do not match the captured venue inventory")
                    venue_symbols.update({(venue, item): symbol for item, symbol in mapping.items()})
                except (ValueError, TypeError, KeyError) as exc:
                    issues.append(f"{venue} inventory: {exc}")
        if {venue for venue, _ in instruments} != VENUES:
            issues.append("scope: both venue inventories must be represented")
    cases = rows(screen, "rows", ("venue", "instrument_id", "funnel"), "screen")
    compare(cases.keys(), {(*key, funnel) for key in instruments for funnel in FUNNELS}, "screen")
    promoted = set()
    for key, row in cases.items():
        label = "screen " + "/".join(key)
        if row.get("status") == "NOT_APPLICABLE":
            source(row.get("source"), label)
            if key[-1] != "FUNNEL-05" or instruments.get(key[:2], {}).get("asset_class") != "crypto":
                issues.append(f"{label}: unverified funnel inapplicability")
        else:
            if re.search(r"concurrency|slot.{0,12}occup|gate\s*[- ]?9", str(row.get("reason", "")), re.I):
                issues.append(f"{label}: portfolio capacity cannot decide a discovery funnel")
            screen_observations(row, key[-1], venue_symbols.get(key[:2], key[1]), label, observed_source, issues)
        if row.get("status") == "HIT":
            promoted.add(key[:2])
        if row.get("status") not in {"HIT", "NO_HIT", "NOT_APPLICABLE"} or not row.get("reason"):
            issues.append(f"{label}: missing resolved disposition or reason")

    managed = rows(scope, "managed", ("venue", "instrument_id"), "managed")
    if scope and "managed" not in scope:
        issues.append("scope: missing managed instrument rows")
    for key, row in managed.items():
        label = "managed " + "/".join(key)
        body = source(row.get("source"), label)
        symbol = venue_symbols.get(key)
        if symbol is None:
            issues.append(f"{label}: instrument ID is absent from the reconciled venue inventory")
        elif body is not None:
            try:
                book_instrument(body, *key, symbol)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                issues.append(f"{label}: {exc}")
    positions = rows(scope, "desk_positions", ("venue", "ticket"), "desk positions")
    if scope and "desk_positions" not in scope:
        issues.append("scope: missing desk position roster")
    for (venue, ticket), row in positions.items():
        label = f"desk position {venue}/{ticket}"
        source(row.get("source"), label)
        pair = (venue, row.get("instrument_id"))
        if not isinstance(pair[1], str) or pair not in managed:
            issues.append(f"{label}: position missing from managed scope")
        if row.get("asset_class") not in {"crypto", "tradfi"}:
            issues.append(f"{label}: missing asset classification")
    crypto_positions = sum(row.get("asset_class") == "crypto" for row in positions.values())
    metrics = rows(telemetry, "rows", ("venue", "instrument_id", "dimension"), "telemetry")
    compare(metrics.keys(), {(*key, dim) for key in promoted | managed.keys() for dim in DIMENSIONS}, "telemetry")
    for key, row in metrics.items():
        label = "telemetry " + "/".join(key)
        dimension = key[-1]
        instrument = venue_symbols.get(key[:2], key[1])
        if (row.get("status") == "NOT_APPLICABLE" and dimension == "D13" and row.get("reason")
                and not re.match(r"^(BTC|ETH)(?:USD|USDT)?(?:\.|$)", instrument, re.I)):
            source(row.get("source"), label)
            continue
        receipt = observed_source(row.get("source"), label, DIMENSION_FEEDS.get(dimension, set()), instrument)
        if row.get("status") != "VALID" or row.get("value") is None or not row.get("unit"):
            issues.append(f"{label}: unresolved value")
        try:
            observed = utc(row["source_event_utc"])
            received = utc(row["received_utc"])
            fresh_for = row["freshness_seconds"]
            if (cutoff is None or type(fresh_for) not in (int, float) or not math.isfinite(fresh_for)
                    or not 0 <= (cutoff - observed).total_seconds() <= fresh_for
                    or not observed <= received <= cutoff):
                raise ValueError("stale or outside cutoff")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            issues.append(f"{label}: invalid freshness ({exc})")
        if key[-1] == "D11":
            windows = row.get("windows")
            if not isinstance(windows, dict) or set(windows) != {"3d", "7d", "30d"}:
                issues.append(f"{label}: missing separate 3d/7d/30d payloads")
            else:
                for window, reference in windows.items():
                    source(reference, f"{label}/{window}")
        if key[-1] == "D16" and (
                type(row.get("value")) not in (int, float)
                or not math.isfinite(row["value"]) or row["value"] < 0
                or row.get("unit") != "USD" or row.get("window_seconds") != 300):
            issues.append(f"{label}: requires five-minute liquidation USD")
        if receipt:
            body, _, captured_at, provider_at = receipt
            try:
                if utc(row["received_utc"]) != captured_at or utc(row["source_event_utc"]) != provider_at:
                    raise ValueError("row timestamps do not match the capture and provider")
                if dimension == "D16":
                    if not math.isclose(row["value"], liquidation_pace(body, captured_at),
                                        rel_tol=1e-9, abs_tol=0.01):
                        raise ValueError("five-minute liquidation USD differs from the captured event sum")
                elif direct_value(body, row.get("value_pointer")) != row.get("value"):
                    raise ValueError("row value differs from its captured JSON field")
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                issues.append(f"{label}: {exc}")
        if dimension == "D11" and isinstance(row.get("windows"), dict):
            for window, reference in row["windows"].items():
                observed_source(reference, f"{label}/{window}", {"liqlevels"}, instrument, window)

    decisions = rows(gates, "rows", ("venue", "instrument_id", "rule"), "gates")
    expected_gates = {(*key, rule) for key in promoted for rule in ENTRY_RULES}
    expected_gates |= {("round", "round", f"C-{n:02d}") for n in range(1, 11)}
    compare(decisions.keys(), expected_gates, "gates")
    for key, row in decisions.items():
        label = "gates " + "/".join(key)
        source(row.get("source"), label)
        if row.get("status") not in {"PASS", "FAIL", "N/A"} or not row.get("reason"):
            issues.append(f"{label}: unresolved rule or missing reason")
        if "observed" not in row or "threshold" not in row:
            issues.append(f"{label}: missing observed input or threshold")
        if key[-1] == "GATE-09" and row.get("status") == "PASS" and (
                len(positions) > 2 or crypto_positions > 1):
            issues.append(f"{label}: PASS contradicts {len(positions)} desk positions, {crypto_positions} crypto")

    # Preserve the original R640 files and reconcile the contradictions they report.
    manifest = read(directory / "step3_manifest.json", "legacy manifest", required=False)
    old_gates = read(directory / "step6_gates.json", "legacy gates", required=False)
    old_dossier = read(directory / "step5_dossiers.json", "legacy dossier", required=False)
    legacy_findings(manifest, old_gates, old_dossier, issues)
    return issues, sorted(checked)


def legacy_findings(manifest, gates, dossier, issues):
    exposure = manifest.get("concurrency_state", {})
    reported_pass = any(
        str(value.get("GATE-09_dynamic_multi_slot", "")).startswith("PASS")
        for key, value in gates.items()
        if key.startswith("socratic_gates_") and isinstance(value, dict)
    )
    if isinstance(exposure, dict) and reported_pass:
        owned = exposure.get("total_owned_positions", 0)
        crypto = exposure.get("crypto_owned_positions", 0)
        if type(owned) is int and type(crypto) is int and (owned > 2 or crypto > 1):
            issues.append(f"legacy GATE-09 contradiction: {owned} owned vs cap 2; {crypto} crypto vs cap 1")
    candidate = dossier.get("promoted_candidate", {})
    if not isinstance(candidate, dict):
        return
    dimensions = candidate.get("dimensions_summary", {})
    if isinstance(dimensions, dict) and dimensions.get("D16_oracle"):
        issues.append("legacy D16 contains the Oracle instead of five-minute liquidation pace")
    if candidate.get("venue") != "Delta Exchange India":
        return
    label = f"legacy {candidate.get('symbol', 'Delta candidate')}"
    try:
        entry, stop, target = (Decimal(str(candidate[key])) for key in ("planned_entry", "planned_sl", "planned_tp"))
        side = "short" if stop > entry else "long"
        oracle = candidate.get("athena_oracle")
        if type(oracle) in (int, float) and (
                (side == "short" and oracle > 35) or (side == "long" and oracle < 65)):
            issues.append(f"{label} {side}: Oracle {oracle} fails the required Delta tail")
        reward = (entry - target) / ((stop - entry) * Decimal("1.20"))
        if reward < Decimal("2.50"):
            issues.append(f"{label} {side}: {reward:.2f}R before fees on the stressed stop, below 2.50R")
    except (KeyError, InvalidOperation, ZeroDivisionError):
        issues.append(f"{label}: malformed price geometry")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"R[0-9]+", args.round):
        parser.error("--round must be R followed by digits")
    try:
        spec = load_round_spec(SPEC)
        issues, checked = verify(ROOT / "data" / "round-evidence" / args.round, args.round, spec)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        spec, issues, checked = {}, [str(exc)], []
    print(json.dumps({
        "round_id": args.round, "spec_sha256": spec.get("sha256"),
        "status": "INCOMPLETE" if issues else "EVIDENCE_CHECKS_PASSED",
        "round_outcome": "INCOMPLETE" if issues else "UNATTESTED",
        "issues": issues, "checked_files": checked,
    }, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    sys.exit(main())
