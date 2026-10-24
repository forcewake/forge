"""Export the versioned lane schemas, or check the committed export."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import models
from .policy import ExecutionPolicy

WIRE_TYPES = (
    models.LaneDispatch,
    models.SessionRequest,
    models.SessionToken,
    models.Brief,
    models.ControlCommand,
    models.ControlDelivery,
    models.ControlAck,
    models.CheckpointEntry,
    models.CheckpointManifest,
    models.CandidateBundle,
    models.LaneReport,
    models.ModelKeyGrant,
    models.Heartbeat,
)


def export(directory: Path, *, check: bool = False) -> list[str]:
    expected = {
        f"{model.__name__}.json": json.dumps(model.model_json_schema(), indent=2, sort_keys=True)
        + "\n"
        for model in (*WIRE_TYPES, ExecutionPolicy)
    }
    mismatches = []
    if not check:
        directory.mkdir(parents=True, exist_ok=True)
    for name, content in expected.items():
        path = directory / name
        if check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                mismatches.append(name)
        else:
            path.write_text(content, encoding="utf-8")
    mismatches.extend(
        sorted(path.name for path in directory.glob("*.json") if path.name not in expected)
    )
    return mismatches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("packages/forge-protocol/schemas"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    mismatches = export(args.directory, check=args.check)
    if mismatches:
        parser.exit(1, "protocol schemas differ: " + ", ".join(mismatches) + "\n")


if __name__ == "__main__":
    main()
