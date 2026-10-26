#!/usr/bin/env python3
"""Replay cached workbook inputs and compare every exported numeric column."""

import argparse
import csv
import json
import math
from pathlib import Path
import sys

from layer_algorithm import LayerAdaptiveController, LayerSettings, WORKBOOK_SHA256


def replay(fixture, settings):
    controller = LayerAdaptiveController(settings)
    results = []
    mismatches = []
    checks = 0
    columns = fixture.get("columns") or [key for key in fixture["rows"][0] if key not in ("reset", "restart")]
    for expected in fixture["rows"]:
        result = controller.process(expected["A"], expected["B"], expected["C"], restart=expected.get("reset", expected.get("restart", False)))
        for column in columns:
            actual, reference = result.diagnostics[column], expected[column]
            same = actual is None if reference is None else (actual is not None and
                math.isclose(actual, reference, rel_tol=1e-10, abs_tol=1e-9))
            checks += 1
            if not same:
                mismatches.append(dict(layer=expected["A"], column=column, expected=reference, actual=actual))
        results.append(dict(result.diagnostics, expectedD=expected["D"],
                            recommendationMatches=result.power_percent == expected["D"], reason=result.reason))
    summary = dict(layers=len(results), numericChecks=checks, mismatchCount=len(mismatches),
                   workbookSha256=fixture.get("sha256"), implementationSourceSha256=WORKBOOK_SHA256)
    return summary, mismatches, results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=Path(__file__).with_name("random80_validation.json"))
    parser.add_argument("--output", type=Path, help="optional UTF-8 BOM CSV for manual review")
    args = parser.parse_args(argv)
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        settings = LayerSettings.from_dict(json.loads(args.settings.read_text(encoding="utf-8")))
        summary, mismatches, results = replay(fixture, settings)
        if args.output:
            with args.output.open("w", newline="", encoding="utf-8-sig") as output:
                columns = fixture.get("columns") or [key for key in fixture["rows"][0] if key not in ("reset", "restart")]
                writer = csv.DictWriter(output, fieldnames=[*columns, "expectedD", "recommendationMatches", "reason"])
                writer.writeheader()
                writer.writerows(results)
        print(json.dumps(summary))
        for mismatch in mismatches[:20]:
            print(json.dumps(mismatch))
        return 1 if mismatches else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Replay failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
