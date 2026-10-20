"""Builds a job input of a given size. Tool-specific: replace with your tool's version.

"Size" is whatever drives your tool's cost and whatever the input schema's maxItems
will limit (for EZKinetics, enzyme-substrate pairs). Use realistic content: a
repeated short example can be much cheaper than real inputs of the same count.
Inputs are written straight to job.json, so they may exceed the schema's current
maxItems; that's how you find where the limit should be.
"""
import json
from pathlib import Path

_EXAMPLE = json.loads((Path(__file__).resolve().parent.parent / "examples" / "job.json").read_text())


def make_job(size: int) -> dict:
    """The sample tool's input with `size` sequences, cycled from examples/job.json."""
    examples = _EXAMPLE["sequences"]
    return {"sequences": [
        dict(examples[i % len(examples)], id=f"seq-{i}") for i in range(size)
    ]}
