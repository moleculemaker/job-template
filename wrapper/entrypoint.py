"""mmli-backend job contract.

Reads the validated job input from $JOB_INPUT_DIR/job.json, calls tool.run(), and writes
the returned results to $JOB_OUTPUT_DIR/results.json. Any uncaught exception exits
non-zero, which the backend records as a failed job.

This file should not need to change between tools; put tool-specific code in tool.py.
"""
import json
import os
from pathlib import Path

from wrapper import tool


def main() -> None:
    input_dir = Path(os.environ["JOB_INPUT_DIR"])
    output_dir = Path(os.environ["JOB_OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)

    job = json.loads((input_dir / "job.json").read_text())
    results = tool.run(job, input_dir, output_dir)

    # Written last and renamed into place, so results.json exists only if the tool finished
    tmp = output_dir / "results.json.tmp"
    tmp.write_text(json.dumps(results, indent=2) + "\n")
    tmp.rename(output_dir / "results.json")


if __name__ == "__main__":
    main()
