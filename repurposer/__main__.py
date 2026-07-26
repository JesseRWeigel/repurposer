"""Command-line entry point for repurposer."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .engine import RepurposerError, compile_document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repurposer",
        description="Compile a canonical JSON document into six source-grounded formats.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    compile_parser = subparsers.add_parser(
        "compile",
        help="compile a source document using declarative transforms",
    )
    compile_parser.add_argument("source", type=Path, help="canonical source JSON")
    compile_parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="transform configuration JSON",
    )
    compile_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="directory for generated files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = compile_document(args.source, args.config, args.output_dir)
    except RepurposerError as exc:
        print(f"repurposer: {exc.code}: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"repurposer: IO_ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"compiled {len(result.outputs)} formats to {result.output_dir}")
    for format_name, filename in result.outputs:
        print(f"{format_name}: {filename}")
    print("manifest: manifest.json")
    for warning in result.warnings:
        print(f"repurposer: WARNING: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
