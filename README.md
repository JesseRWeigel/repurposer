# repurposer

A single canonical document compiles to newsletter HTML, RSS, a podcast script, a vertical video storyboard, a carousel, and a plain-text version, with per-format transforms declared in config rather than done by re-prompting. Formats that would require inventing facts not present in the source fail loudly instead of hallucinating filler.

Catalog task: `MEDIA-046`. Part of [thousand](../../README.md).

## What this is

Repurposer is a deterministic command-line compiler. It reads one structured JSON document and
a JSON transform configuration, then writes newsletter HTML, RSS, a podcast script, a vertical
video storyboard, a carousel, and plain text. Every output is derived from source fields selected
in configuration.

Assumption: "canonical document" means structured source content with explicit metadata,
sections, and optional facts. JSON keeps the format inspectable and avoids a runtime dependency.

## Running it

```bash
python3 -m repurposer compile examples/source.json \
  --config examples/transforms.json \
  --output-dir build
```

The verification command is defined before implementation and exercises the real CLI, all six
renderers, output validation, stale-output cleanup, and failure on a requested missing fact:

```bash
python3 -m unittest discover -s tests -v && python3 scripts/verify_real_behavior.py
```

## Status

**NOT YET VERIFIED.** No verify command has been run against this project.

A maintainer or agent must replace this section with the pasted output of the verify
command. Per `AGENTS.md`, a project is not done until that output appears here and
`tools/logrun.py` has recorded exit code 0.

## Unfinished

- Implementation has not started.
