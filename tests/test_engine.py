from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
