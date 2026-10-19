"""Sample tool: protein sequence length and average molecular weight.

Replace this module with a call into the method author's code. run() receives the job
input (already validated by the backend against schemas/input.schema.json), may read
uploaded files from input_dir, may write artifact files to output_dir, and returns the
object that becomes results.json (validated in CI against schemas/output.schema.json).
"""
import csv
from pathlib import Path

# Average residue masses (Da), i.e. amino acid mass minus water
RESIDUE_MASS = {
    "A": 71.0788, "R": 156.1875, "N": 114.1038, "D": 115.0886, "C": 103.1388,
    "E": 129.1155, "Q": 128.1307, "G": 57.0519, "H": 137.1411, "I": 113.1594,
    "L": 113.1594, "K": 128.1741, "M": 131.1926, "F": 147.1766, "P": 97.1167,
    "S": 87.0782, "T": 101.1051, "W": 186.2132, "Y": 163.1760, "V": 99.1326,
}
WATER_MASS = 18.01528


def run(job: dict, input_dir: Path, output_dir: Path) -> dict:
    rows = []
    for entry in job["sequences"]:
        sequence = entry["sequence"].upper()
        weight = sum(RESIDUE_MASS[residue] for residue in sequence) + WATER_MASS
        rows.append({
            "id": entry["id"],
            "length": len(sequence),
            "molecular_weight_da": round(weight, 2),
        })

    with open(output_dir / "results.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "length", "molecular_weight_da"])
        writer.writeheader()
        writer.writerows(rows)

    return {
        "rows": rows,
        "artifacts": [
            {"name": "results.csv", "media_type": "text/csv", "description": "All rows as CSV"},
        ],
    }
