#!/usr/bin/env python3
"""Read-only Antigravity wake check and digest-pinned SOP section reader."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "desk" / "CANONICAL_MASTER_SPECIFICATION.md"
BOOT = ROOT / "scripts" / "round_preflight_bootstrap.py"
ANCHOR = re.compile(r'<a id="([a-z0-9-]+)"></a>')
ROUND_ID = re.compile(r"R[0-9]+")
FORMULATION_STEP = 5
COMPANIONS = {
    "launch-prompt": "CANONICAL_PROMPT.txt",
    "evidence-contract": "ROUND_EVIDENCE_CONTRACT.md",
}
REQUIRED = {"kernel", "boot", "sec-4b", *(f"sec-{i}" for i in range(26)),
            *(f"appendix-{letter}" for letter in "abcdefg")}


def load_spec(path):
    raw = path.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    starts = {}
    for i, line in enumerate(lines):
        match = ANCHOR.fullmatch(line.strip())
        if match is None:
            continue
        key = match.group(1)
        if key in starts or i == 0 or not lines[i - 1].startswith("## "):
            raise ValueError(f"Invalid or duplicate section anchor: {key}")
        starts[key] = i - 1
    missing = REQUIRED - starts.keys()
    if missing:
        raise ValueError(f"Missing section anchors: {', '.join(sorted(missing))}")
    if next(iter(starts)) != "kernel":
        raise ValueError("The Kernel must be the first SOP section")
    starts["kernel"] = 0

    sections = {}
    anchors = list(starts.items())
    for i, (key, start) in enumerate(anchors):
        end = anchors[i + 1][1] if i + 1 < len(anchors) else len(lines)
        # These ranges partition the exact document, including navigation and EOF.
        body = "".join(lines[start:end])
        sections[key] = {
            "start_line": start + 1,
            "end_line": end,
            "section_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "body": body,
        }
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "sections": sections,
    }


def section_index(spec):
    return {key: {field: value for field, value in section.items() if field != "body"}
            for key, section in spec["sections"].items()}


def load_round_spec(path):
    spec = load_spec(path)
    spec["section_sources"] = dict.fromkeys(spec["sections"], f"desk/{path.name}")
    spec["document_hashes"] = {}
    for anchor, filename in COMPANIONS.items():
        raw = (path.parent / filename).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        body = raw.decode("utf-8-sig")
        spec["document_hashes"][filename] = digest
        spec["section_sources"][anchor] = f"desk/{filename}"
        spec["sections"][anchor] = {
            "start_line": 1, "end_line": len(body.splitlines()),
            "section_sha256": digest, "body": body,
        }
    stages = {}
    for step, cell in re.findall(r"(?m)^\| (\d+) · [^|]+\| ([^|]+)\|", spec["sections"]["kernel"]["body"]):
        number = int(step)
        anchors = re.findall(r"`([a-z0-9-]+)`", cell)
        if (number in stages or not anchors or len(anchors) != len(set(anchors))
                or ("kernel" in anchors) != (number == 0)
                or set(anchors) - spec["sections"].keys()):
            raise ValueError(f"Invalid canonical anchor table at Step {step}")
        stages[number] = [anchor for anchor in anchors if anchor != "kernel"]
    if set(stages) != set(range(12)) or not all(stages.values()):
        raise ValueError("Canonical anchor table must define Steps 0–11")
    pre_formulation = {"kernel"}
    for number in range(FORMULATION_STEP + 1):
        pre_formulation.update(stages[number])
    unassigned = spec["sections"].keys() - pre_formulation
    if unassigned:
        raise ValueError("Sections missing before formulation: " + ", ".join(sorted(unassigned)))
    spec["stages"] = stages
    return spec


def timestamp(value):
    date = datetime.fromisoformat(value)
    if date.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("Expected a UTC timestamp")
    return date


def read_receipt(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a receipt object in {path.name}")
    return data


def validate_wake(wake, round_id, spec):
    boot = wake.get("bootstrap")
    report = boot.get("report") if isinstance(boot, dict) else None
    wake_id = wake.get("wake_id")
    if (wake.get("round_id") != round_id or wake.get("kind") != "wake"
            or wake.get("status") != "REPORTED" or wake.get("spec_sha256") != spec["sha256"]
            or wake.get("canonical_spec", spec["section_sources"]["kernel"]) != spec["section_sources"]["kernel"]
            or wake.get("document_hashes") != spec["document_hashes"]
            or wake.get("sections_read") != {"kernel": section_index(spec)["kernel"]}
            or not isinstance(wake_id, str) or not re.fullmatch(r"[0-9a-f]{32}", wake_id)
            or not isinstance(report, dict)
            or any(not isinstance(report.get(name), dict) for name in ("spec", "terminal_engine", "mt5_status"))):
        raise ValueError("Wake receipt differs from the current protocol or pinned files. Keep the archive and use a new round ID.")
    return timestamp(wake["observed_utc"])


def section_progress(directory, wake, spec, wake_time):
    reads = {step: {} for step in spec["stages"]}
    problems = []
    index = section_index(spec)
    for path in sorted((directory / "section_receipts").glob("*.json")):
        try:
            receipt = read_receipt(path)
            step = receipt.get("step")
            anchors = receipt.get("sections_read")
            at = timestamp(receipt["observed_utc"])
            if (type(step) is not int or step not in reads or not isinstance(anchors, dict) or not anchors
                    or receipt.get("kind") != "section_read" or receipt.get("status") != "OK"
                    or any(receipt.get(k) != wake[k] for k in ("wake_id", "round_id", "spec_sha256", "document_hashes"))
                    or at < wake_time
                    or any(key not in spec["stages"][step] or index[key] != item for key, item in anchors.items())):
                raise ValueError("receipt does not match the wake, time, step or anchors")
            if "source_files" in receipt and receipt["source_files"] != {
                    key: spec["section_sources"][key] for key in anchors}:
                raise ValueError("section source paths do not match the pinned documents")
            for key in anchors:
                reads[step][key] = min(at, reads[step].get(key, at))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            problems.append(f"{path.name}: {exc}")
    missing = {n: [key for key in spec["stages"][n] if key not in reads[n]] for n in reads}
    previous_end = wake_time
    for step, anchors in reads.items():
        if anchors and (previous_end is None or min(anchors.values()) < previous_end):
            problems.append(f"Step {step} reads predate completion of the preceding sections")
        previous_end = max(anchors.values()) if not missing[step] and anchors else None
    return missing, problems


def sop_progress(spec, missing):
    read = {"kernel"}
    for step, anchors in spec["stages"].items():
        read.update(set(anchors) - set(missing[step]))
    sop_sections = {key: section for key, section in spec["sections"].items() if key not in COMPANIONS}
    unread = [key for key in sop_sections if key not in read]
    total = sum(section["end_line"] - section["start_line"] + 1 for section in sop_sections.values())
    return {
        "lines_read": total - sum(sop_sections[key]["end_line"] - sop_sections[key]["start_line"] + 1 for key in unread),
        "total_lines": total,
        "unread_anchors": unread,
        "formulation_reads_complete": not unread and not any(missing[n] for n in range(FORMULATION_STEP + 1)),
    }


def save_receipt(path, payload):
    """Publish a complete receipt without replacing prior evidence, even on retries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8") as handle:
        temp = Path(handle.name)
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.link(temp, path)
        except FileExistsError:
            return read_receipt(path)
        return payload
    finally:
        temp.unlink()


def bootstrap():
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(BOOT)],
            cwd=ROOT,
            capture_output=True,
            encoding="utf-8",
            timeout=90,
            check=True,
        )
        report = json.loads(proc.stdout)
        if not isinstance(report, dict):
            raise ValueError("Expected a JSON object from bootstrap")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"state": "ERROR", "error": str(exc),
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
    return {"state": "REPORTED", "elapsed_ms": round((time.monotonic() - started) * 1000),
            "report": report}


def emit(payload):
    print("WAKE_RECEIPT " + json.dumps(payload, ensure_ascii=False, allow_nan=False))


def emit_sections(spec, anchors):
    for anchor in anchors:
        section = spec["sections"][anchor]
        print(f"--- SOP SECTION {anchor} ---")
        print(f"Source: {spec['section_sources'][anchor]}:{section['start_line']}-{section['end_line']}")
        body = section["body"]
        print(body, end="" if body.endswith(("\n", "\r")) else "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", required=True, help="Scheduler-supplied round ID")
    parser.add_argument("--section", action="append", help="Read one anchor, may be repeated")
    parser.add_argument("--expect-sha256", help="Optional independent check against the pinned SOP")
    parser.add_argument("--save", action="store_true", help="Accepted for compatibility; round receipts are always saved")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--step", type=int, choices=range(12), help="Read the next missing anchor for this step")
    operation.add_argument("--status", action="store_true", help="List missing anchors without running probes")
    operation.add_argument("--require-formulation-reads", action="store_true",
                           help="Fail unless the full SOP and all Step 0–5 inputs were read")
    args = parser.parse_args(argv)
    if not ROUND_ID.fullmatch(args.round):
        parser.error("--round must be R followed by digits")
    if args.section and args.step is None:
        parser.error("--section is used with --step")

    directory = ROOT / "data" / "round-evidence" / args.round
    path = directory / "wake_receipt.json"
    payload = {"round_id": args.round}
    try:
        spec = load_round_spec(SPEC)
        payload["spec_sha256"] = spec["sha256"]
        payload["canonical_spec"] = spec["section_sources"]["kernel"]
        if args.expect_sha256 and args.expect_sha256 != spec["sha256"]:
            raise ValueError("SOP differs from the supplied hash. Use a new round ID if the SOP changed.")
        index = section_index(spec)
        now = datetime.now(timezone.utc).isoformat()
        if args.step is None and not (args.status or args.require_formulation_reads):
            if path.exists():
                payload = read_receipt(path)
                validate_wake(payload, args.round, spec)
            else:
                payload.update(
                    kind="wake", wake_id=uuid4().hex, spec_size_bytes=spec["size_bytes"],
                    document_hashes=spec["document_hashes"], observed_utc=now,
                    sections_read={"kernel": index["kernel"]}, section_index=index,
                    unchecked=["skill_loading", "delta_mcp_call", "venue_reconciliation",
                               "telemetry_coverage", "risk_gates", "execution_authorization", "scheduler"],
                )
                payload["bootstrap"] = bootstrap()
                if payload["bootstrap"]["state"] != "REPORTED":
                    raise ValueError(payload["bootstrap"]["error"])
                payload["status"] = "REPORTED"
                validate_wake(payload, args.round, spec)
                payload = save_receipt(path, payload)
                validate_wake(payload, args.round, spec)
            emit_sections(spec, ["kernel"])
        else:
            if not path.is_file():
                raise ValueError("Wake receipt missing. Start with the round wake command.")
            wake = read_receipt(path)
            wake_time = validate_wake(wake, args.round, spec)
            missing, problems = section_progress(directory, wake, spec, wake_time)
            if problems:
                raise ValueError("; ".join(problems))
            if args.status or args.require_formulation_reads:
                progress = sop_progress(spec, missing)
                payload.update(kind="section_progress", status="OK", missing=missing,
                               wake_observed_utc=wake["observed_utc"],
                               section_steps_complete=sum(not anchors for anchors in missing.values()),
                               sop_coverage=progress)
                if args.require_formulation_reads and not progress["formulation_reads_complete"]:
                    raise ValueError("Full SOP and Step 0–5 reads are required before formulation")
            else:
                step = args.step
                preceding = next((n for n in range(step) if missing[n]), None)
                if preceding is not None:
                    raise ValueError(f"Step {preceding} still needs: {', '.join(missing[preceding])}")
                anchors = list(dict.fromkeys(args.section or missing[step][:1]))
                unknown = set(anchors) - set(spec["stages"][step])
                if unknown:
                    raise ValueError(f"Step {step} does not require: {', '.join(sorted(unknown))}")
                if anchors:
                    payload.update(kind="section_read", wake_id=wake["wake_id"], step=step,
                                   document_hashes=spec["document_hashes"],
                                   observed_utc=datetime.now(timezone.utc).isoformat(),
                                   sections_read={key: index[key] for key in anchors},
                                   source_files={key: spec["section_sources"][key] for key in anchors}, status="OK")
                    name = f"{step:02d}-{'-'.join(sorted(anchors))}.json"
                    payload = save_receipt(directory / "section_receipts" / name, payload)
                    emit_sections(spec, anchors)
                else:
                    payload.update(kind="section_progress", step=step, status="STEP_READY")
                payload["remaining_anchors"] = [key for key in missing[step] if key not in anchors]
                missing[step] = payload["remaining_anchors"]
                payload["sop_coverage"] = sop_progress(spec, missing)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        payload.update(status="ERROR", error=str(exc))
    emit(payload)
    return int(payload["status"] == "ERROR")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
