#!/usr/bin/env python3
"""Check a job's output against the contract.

Usage: validate.py OUTPUT_DIR

- examples/job.json validates against schemas/input.schema.json
- OUTPUT_DIR/results.json validates against schemas/output.schema.json
- every artifact named in results.json exists in OUTPUT_DIR
- if examples/expected/ exists, results.json and each artifact match it exactly
  (delete examples/expected/ for tools whose output is not deterministic)
"""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent


def validate(instance_path: Path, schema_path: Path) -> list[str]:
    validator = Draft202012Validator(json.loads(schema_path.read_text()))
    instance = json.loads(instance_path.read_text())
    return [f"{instance_path.name}: {e.json_path}: {e.message}" for e in validator.iter_errors(instance)]


def main(output_dir: Path) -> int:
    errors = validate(ROOT / "examples" / "job.json", ROOT / "schemas" / "input.schema.json")

    results_path = output_dir / "results.json"
    if not results_path.exists():
        print(f"FAIL: {results_path} was not written")
        return 1
    errors += validate(results_path, ROOT / "schemas" / "output.schema.json")

    artifacts = [a["name"] for a in json.loads(results_path.read_text()).get("artifacts", [])]
    errors += [f"artifact {name} is listed in results.json but missing" for name in artifacts
               if not (output_dir / name).is_file()]

    expected_dir = ROOT / "examples" / "expected"
    if expected_dir.is_dir():
        if json.loads(results_path.read_text()) != json.loads((expected_dir / "results.json").read_text()):
            errors.append("results.json differs from examples/expected/results.json")
        for name in artifacts:
            expected = expected_dir / name
            if expected.is_file() and (output_dir / name).is_file() \
                    and expected.read_bytes() != (output_dir / name).read_bytes():
                errors.append(f"{name} differs from examples/expected/{name}")

    for error in errors:
        print(f"FAIL: {error}")
    if not errors:
        print("OK: output satisfies the contract")
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1])))
