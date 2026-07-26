from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from repurposer import RepurposerError, compile_document


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT_ROOT / "examples" / "source.json"
CONFIG = PROJECT_ROOT / "examples" / "transforms.json"


class CompilerTests(unittest.TestCase):
    def test_cli_compiles_all_formats_and_hashes_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "out"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "repurposer",
                    "compile",
                    str(SOURCE),
                    "--config",
                    str(CONFIG),
                    "--output-dir",
                    str(output_dir),
                ],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("compiled 6 formats", completed.stdout)

            manifest = json.loads((output_dir / "manifest.json").read_text())
            self.assertEqual(len(manifest["outputs"]), 6)
            for entry in manifest["outputs"]:
                content = (output_dir / entry["filename"]).read_bytes()
                self.assertEqual(
                    hashlib.sha256(content).hexdigest(),
                    entry["sha256"],
                )
            feed = ET.parse(output_dir / "feed.xml")
            self.assertEqual(feed.getroot().tag, "rss")

    def test_each_format_applies_its_declared_transforms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "out"
            compile_document(SOURCE, CONFIG, output_dir)
            source = json.loads(SOURCE.read_text())["document"]

            podcast = (output_dir / "podcast-script.txt").read_text()
            self.assertIn(source["sections"][0]["paragraphs"][1], podcast)
            self.assertNotIn(source["sections"][0]["bullets"][0], podcast)

            storyboard = json.loads(
                (output_dir / "vertical-storyboard.json").read_text()
            )
            self.assertEqual(len(storyboard["scenes"]), 4)
            self.assertEqual(
                storyboard["scenes"][1]["voiceover"],
                source["sections"][0]["paragraphs"][0]
                + "\n"
                + source["sections"][0]["bullets"][0],
            )
            self.assertNotIn(
                source["sections"][2]["heading"],
                json.dumps(storyboard),
            )

            carousel = json.loads((output_dir / "carousel.json").read_text())
            self.assertEqual(len(carousel["slides"]), 5)
            self.assertEqual(
                carousel["slides"][1]["body"],
                [
                    source["sections"][0]["paragraphs"][0],
                    *source["sections"][0]["bullets"],
                ],
            )

    def test_missing_source_fails_before_touching_existing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "out"
            compile_document(SOURCE, CONFIG, output_dir)
            before = {
                path.name: path.read_bytes()
                for path in output_dir.iterdir()
                if path.is_file()
            }
            bad_source = json.loads(SOURCE.read_text())
            del bad_source["document"]["summary"]
            bad_source_path = root / "bad-source.json"
            bad_source_path.write_text(json.dumps(bad_source))

            with self.assertRaises(RepurposerError) as caught:
                compile_document(bad_source_path, CONFIG, output_dir)
            self.assertEqual(caught.exception.code, "SOURCE_GAP")
            self.assertIn("document.summary", str(caught.exception))
            after = {
                path.name: path.read_bytes()
                for path in output_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_html_escapes_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = json.loads(SOURCE.read_text())
            source["document"]["title"] = "Research <review> & notes"
            source_path = root / "source.json"
            source_path.write_text(json.dumps(source))
            output_dir = root / "out"
            compile_document(source_path, CONFIG, output_dir)
            newsletter = (output_dir / "newsletter.html").read_text()
            self.assertIn("Research &lt;review&gt; &amp; notes", newsletter)
            self.assertNotIn("Research <review> & notes", newsletter)

    def test_renamed_output_removes_only_manifest_owned_stale_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "out"
            compile_document(SOURCE, CONFIG, output_dir)
            unrelated = output_dir / "notes.keep"
            unrelated.write_text("owner content")

            config = json.loads(CONFIG.read_text())
            config["formats"]["newsletter_html"]["output"] = "newsletter-v2.html"
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            compile_document(SOURCE, config_path, output_dir)

            self.assertFalse((output_dir / "newsletter.html").exists())
            self.assertTrue((output_dir / "newsletter-v2.html").exists())
            self.assertEqual(unrelated.read_text(), "owner content")

    def test_modified_stale_output_is_preserved_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "out"
            compile_document(SOURCE, CONFIG, output_dir)
            old_output = output_dir / "newsletter.html"
            old_output.write_text("manual edit")

            config = json.loads(CONFIG.read_text())
            config["formats"]["newsletter_html"]["output"] = "newsletter-v2.html"
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            with self.assertRaises(RepurposerError) as caught:
                compile_document(SOURCE, config_path, output_dir)
            self.assertEqual(caught.exception.code, "OUTPUT_CONFLICT")
            self.assertEqual(old_output.read_text(), "manual edit")
            self.assertFalse((output_dir / "newsletter-v2.html").exists())

    def test_config_cannot_inject_free_form_output_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for format_name, option_name in (
                ("vertical_video_storyboard", "visual_direction"),
                ("carousel", "cover_label"),
            ):
                with self.subTest(option=option_name):
                    config = json.loads(CONFIG.read_text())
                    config["formats"][format_name]["options"][option_name] = (
                        "An unsupported factual claim"
                    )
                    config_path = root / f"{option_name}.json"
                    config_path.write_text(json.dumps(config))
                    with self.assertRaises(RepurposerError) as caught:
                        compile_document(SOURCE, config_path, root / option_name)
                    self.assertEqual(caught.exception.code, "INVALID_CONFIG")
                    self.assertIn("unsupported options", str(caught.exception))

    def test_transform_cannot_leave_an_empty_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = json.loads(SOURCE.read_text())
            source["document"]["sections"][0] = {
                "heading": "Bullet-only section",
                "paragraphs": [],
                "bullets": ["This content must survive or compilation must fail."]
            }
            source_path = root / "source.json"
            source_path.write_text(json.dumps(source))
            with self.assertRaises(RepurposerError) as caught:
                compile_document(source_path, CONFIG, root / "out")
            self.assertEqual(caught.exception.code, "EMPTY_FORMAT")
            self.assertIn("Bullet-only section", str(caught.exception))

    def test_malformed_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, malformed in enumerate(
                (
                    "https://exa mple.com/not-valid",
                    "https://example.com／not-valid",
                )
            ):
                with self.subTest(url=malformed):
                    source = json.loads(SOURCE.read_text())
                    source["document"]["canonical_url"] = malformed
                    source_path = root / f"source-{index}.json"
                    source_path.write_text(json.dumps(source))
                    with self.assertRaises(RepurposerError) as caught:
                        compile_document(source_path, CONFIG, root / f"out-{index}")
                    self.assertEqual(caught.exception.code, "INVALID_SOURCE")

    def test_newsletter_language_must_come_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = json.loads(SOURCE.read_text())
            del source["document"]["language"]
            source_path = root / "source.json"
            source_path.write_text(json.dumps(source))
            with self.assertRaises(RepurposerError) as caught:
                compile_document(source_path, CONFIG, root / "out")
            self.assertEqual(caught.exception.code, "SOURCE_GAP")
            self.assertIn("document.language", str(caught.exception))

    def test_xml_invalid_characters_are_rejected_at_every_nesting_level(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = (
                ("summary", "\u0001", "U+0001"),
                ("paragraph", "\u0001", "U+0001"),
                ("bullet", "\ud800", "U+D800"),
            )
            for index, (location, character, expected_codepoint) in enumerate(cases):
                with self.subTest(location=location):
                    source = json.loads(SOURCE.read_text())
                    if location == "summary":
                        source["document"]["summary"] += character
                    elif location == "paragraph":
                        source["document"]["sections"][0]["paragraphs"][0] += (
                            character
                        )
                    else:
                        source["document"]["sections"][0]["bullets"][0] += character
                    source_path = root / f"source-{index}.json"
                    source_path.write_text(json.dumps(source))
                    with self.assertRaises(RepurposerError) as caught:
                        compile_document(source_path, CONFIG, root / f"out-{index}")
                    self.assertEqual(caught.exception.code, "INVALID_SOURCE")
                    self.assertIn(expected_codepoint, str(caught.exception))

    def test_destination_collision_leaves_prior_snapshot_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "out"
            compile_document(SOURCE, CONFIG, output_dir)
            feed = output_dir / "feed.xml"
            feed.unlink()
            feed.mkdir()
            before = {
                path.name: path.read_bytes()
                for path in output_dir.iterdir()
                if path.is_file()
            }

            changed_source = json.loads(SOURCE.read_text())
            changed_source["document"]["title"] = "Changed title"
            changed_source_path = root / "changed.json"
            changed_source_path.write_text(json.dumps(changed_source))
            with self.assertRaises(RepurposerError) as caught:
                compile_document(changed_source_path, CONFIG, output_dir)
            self.assertEqual(caught.exception.code, "OUTPUT_CONFLICT")

            after = {
                path.name: path.read_bytes()
                for path in output_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertTrue(feed.is_dir())

    def test_unexpected_install_failure_rolls_back_every_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "out"
            compile_document(SOURCE, CONFIG, output_dir)
            before = {
                path.name: path.read_bytes()
                for path in output_dir.iterdir()
                if path.is_file()
            }

            changed_source = json.loads(SOURCE.read_text())
            changed_source["document"]["title"] = "Changed title"
            changed_source_path = root / "changed.json"
            changed_source_path.write_text(json.dumps(changed_source))

            real_replace = os.replace
            call_count = 0

            def fail_during_install(source: object, destination: object) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 9:
                    raise OSError("injected install failure")
                real_replace(source, destination)

            with patch(
                "repurposer.engine.os.replace",
                side_effect=fail_during_install,
            ):
                with self.assertRaises(OSError):
                    compile_document(changed_source_path, CONFIG, output_dir)

            after = {
                path.name: path.read_bytes()
                for path in output_dir.iterdir()
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse(
                any(path.suffix in {".tmp", ".backup"} for path in output_dir.iterdir())
            )

    def test_malformed_prior_manifest_returns_controlled_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, invalid_outputs in enumerate((None, 42)):
                with self.subTest(outputs=invalid_outputs):
                    output_dir = root / f"out-{index}"
                    compile_document(SOURCE, CONFIG, output_dir)
                    manifest = output_dir / "manifest.json"
                    manifest.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "outputs": invalid_outputs,
                            }
                        )
                    )
                    with self.assertRaises(RepurposerError) as caught:
                        compile_document(SOURCE, CONFIG, output_dir)
                    self.assertEqual(caught.exception.code, "OUTPUT_CONFLICT")
                    self.assertIn("must be an array", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
