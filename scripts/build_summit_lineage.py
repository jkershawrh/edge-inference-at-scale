#!/usr/bin/env python3
"""Build the deterministic Summit Connect evidence lineage manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from corpus_factory.summit_lineage import (  # noqa: E402
    SummitLineageError,
    build_summit_lineage,
    write_summit_lineage,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--classifications", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        manifest = build_summit_lineage(
            arguments.data_dir,
            arguments.registry,
            arguments.classifications,
        )
        write_summit_lineage(arguments.output, manifest)
    except SummitLineageError as exc:
        parser.exit(2, f"lineage error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
