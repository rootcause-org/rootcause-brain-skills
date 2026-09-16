#!/usr/bin/env python3
"""Read prior v2 reports without importing the renderer or its dependencies."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-\d+d)?$")
INHERITED = (
    "text_en",
    "text_nl",
    "ask_nl",
    "ask_for",
    "prompt",
    "impact",
    "root_cause",
)


def prior_findings(
    report_json_path, *, prior_dirs=None, date=None, report_id=None
) -> dict:
    path = Path(report_json_path)
    current_dir = path.parent if path.suffix == ".json" else path
    date = date or current_dir.name[:10]
    dirs = (
        list(prior_dirs)
        if prior_dirs is not None
        else list(current_dir.parent.glob("*"))
    )
    cadence = re.search(r"-(\d+)d$", current_dir.name)
    cadence = cadence.group(1) if cadence else "1"
    latest = {}
    for directory in sorted(map(Path, dirs), key=lambda p: p.name):
        if not DAY_DIR.fullmatch(directory.name) or directory.name[:10] >= date:
            continue
        prior_cadence = re.search(r"-(\d+)d$", directory.name)
        if (prior_cadence.group(1) if prior_cadence else "1") != cadence:
            continue
        try:
            data = json.loads((directory / "report.json").read_text())
            if not isinstance(data, dict) or data.get("schema_version") != 2:
                continue
            if report_id and data.get("report_id") != report_id:
                continue
            findings = data["findings"]
            if not isinstance(findings, list) or not all(
                isinstance(f, dict) for f in findings
            ):
                continue
            for finding in findings:
                if not all(
                    finding.get(k)
                    for k in ("signature", "title", "severity", "status", "audience")
                ):
                    continue
                signature = finding["signature"]
                old = latest.get(signature)
                resolved = dict(finding)
                prompt_date = directory.name[:10]
                if finding["status"] == "unchanged":
                    if not old:
                        continue
                    resolved.update({k: old["finding"].get(k) for k in INHERITED})
                    prompt_date = old["prompt_date"]
                elif not finding.get("impact") or not finding.get("root_cause"):
                    continue
                latest[signature] = {
                    "date": directory.name[:10],
                    "finding": resolved,
                    "prompt_date": prompt_date,
                }
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return latest


def prior_table(findings: dict) -> str:
    lines = [
        "## Prior findings (reuse these signatures)",
        "",
        "signature | last date | severity | status | audience | title | has prompt",
        "--- | --- | --- | --- | --- | --- | ---",
    ]
    for signature, prior in findings.items():
        f = prior["finding"]
        values = [
            signature,
            prior["date"],
            f["severity"],
            f["status"],
            f["audience"],
            f["title"],
            "yes" if f.get("prompt") else "no",
        ]
        lines.append(
            " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values)
        )
    if not findings:
        lines.append("No prior v2 findings.")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args()
    print(prior_table(prior_findings(args.out_dir)))
