# repurposer

A single canonical document compiles to newsletter HTML, RSS, a podcast script, a vertical video storyboard, a carousel, and a plain-text version, with per-format transforms declared in config rather than done by re-prompting. Formats that would require inventing facts not present in the source fail loudly instead of hallucinating filler.

Catalog task: `MEDIA-046`. Part of [thousand](../../README.md). Repo: none. The fleet lead will
create and push the public GitHub repository from the networked host.

## What this is

Repurposer is a deterministic command-line compiler. It reads one structured JSON document and
a JSON transform configuration, then writes newsletter HTML, RSS, a podcast script, a vertical
video storyboard, a carousel, and plain text. Every output is derived from source fields selected
in configuration.

Assumption: "canonical document" means structured source content with explicit metadata,
sections, and optional facts. JSON keeps the format inspectable and avoids a runtime dependency.

## Requirements

- Python 3.10 or newer
- No third-party packages

## Quick start

```bash
python3 -m repurposer compile examples/source.json \
  --config examples/transforms.json \
  --output-dir build
```

Successful compilation writes these seven files:

| Format | Example output |
| --- | --- |
| Newsletter HTML | `newsletter.html` |
| RSS 2.0 feed | `feed.xml` |
| Podcast script | `podcast-script.txt` |
| Vertical video storyboard | `vertical-storyboard.json` |
| Carousel plan | `carousel.json` |
| Plain text | `plain.txt` |
| Provenance and hashes | `manifest.json` |

## Source and configuration

The source can use any object layout. Each format maps fixed renderer roles such as `title`,
`summary`, and `sections` to dotted paths in that object. See
[`examples/source.json`](examples/source.json) and
[`examples/transforms.json`](examples/transforms.json) for a complete pair.

Every format has its own `transforms` array. Supported operations are:

- `take_sections`
- `take_paragraphs_per_section`
- `take_bullets_per_section`
- `drop_bullets`

Transforms select or omit source content. They never generate replacement copy. A format stops
with a `SOURCE_GAP` error when a mapped path is absent or null. Schema errors, unsupported
operations, duplicate output names, unsafe filenames, and invalid URLs also stop compilation
before output files are written. Renderer options are limited to safe presentation controls.
Free-form configuration strings cannot enter generated copy.

The output manifest records the source and configuration hashes, each generated file hash, the
source paths consumed by each format, and its transforms. Repeated runs are byte-for-byte
deterministic. When an output filename changes, cleanup removes the old file only when its hash
still matches the prior manifest. Unrelated files remain in place. Output installation uses a
rollback transaction, so a destination collision or write failure cannot leave mixed generations.

## Verification

This command exercises the real CLI, all six renderers, independent structure and grounding
checks, deterministic output, safe stale-output cleanup, and failure on a requested missing fact:

```bash
python3 -m unittest discover -s tests -v && python3 scripts/verify_real_behavior.py
```

## Status

Verified with Python 3.12.3. Command:

```bash
python3 -m unittest discover -s tests -v && python3 scripts/verify_real_behavior.py
```

Observed output:

```text
test_cli_compiles_all_formats_and_hashes_match (test_engine.CompilerTests.test_cli_compiles_all_formats_and_hashes_match) ... ok
test_config_cannot_inject_free_form_output_copy (test_engine.CompilerTests.test_config_cannot_inject_free_form_output_copy) ... ok
test_destination_collision_leaves_prior_snapshot_unchanged (test_engine.CompilerTests.test_destination_collision_leaves_prior_snapshot_unchanged) ... ok
test_each_format_applies_its_declared_transforms (test_engine.CompilerTests.test_each_format_applies_its_declared_transforms) ... ok
test_html_escapes_source_content (test_engine.CompilerTests.test_html_escapes_source_content) ... ok
test_malformed_prior_manifest_returns_controlled_error (test_engine.CompilerTests.test_malformed_prior_manifest_returns_controlled_error) ... ok
test_malformed_url_is_rejected (test_engine.CompilerTests.test_malformed_url_is_rejected) ... ok
test_missing_source_fails_before_touching_existing_outputs (test_engine.CompilerTests.test_missing_source_fails_before_touching_existing_outputs) ... ok
test_modified_stale_output_is_preserved_and_reported (test_engine.CompilerTests.test_modified_stale_output_is_preserved_and_reported) ... ok
test_newsletter_language_must_come_from_source (test_engine.CompilerTests.test_newsletter_language_must_come_from_source) ... ok
test_renamed_output_removes_only_manifest_owned_stale_file (test_engine.CompilerTests.test_renamed_output_removes_only_manifest_owned_stale_file) ... ok
test_transform_cannot_leave_an_empty_section (test_engine.CompilerTests.test_transform_cannot_leave_an_empty_section) ... ok
test_unexpected_install_failure_rolls_back_every_output (test_engine.CompilerTests.test_unexpected_install_failure_rolls_back_every_output) ... ok
test_xml_invalid_characters_are_rejected_at_every_nesting_level (test_engine.CompilerTests.test_xml_invalid_characters_are_rejected_at_every_nesting_level) ... ok

----------------------------------------------------------------------
Ran 14 tests in 0.142s

OK
PASS: CLI generated six structurally valid, source-grounded formats
PASS: repeated compilation was byte-for-byte deterministic
PASS: safe stale-output cleanup followed the manifest
PASS: absent source facts failed loudly before output changes
PASS: adversarial config, URLs, nested text, metadata, and transforms were rejected
PASS: output collisions and malformed manifests failed without partial writes
VERIFY PASS: all required behaviors observed
```

## Unfinished

- No incomplete items are known within the `MEDIA-046` task scope.
- Audio and encoded video rendering are outside this compiler. It produces the requested podcast
  script and vertical video storyboard.
- Source documents and transform configuration currently use JSON only.
