"""Exercise the installed CLI and validate outputs without importing its code."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = PROJECT_ROOT / "examples" / "source.json"
CONFIG_PATH = PROJECT_ROOT / "examples" / "transforms.json"


class TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.links: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "a":
            attributes = dict(attrs)
            if attributes.get("href"):
                self.links.append(attributes["href"] or "")

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self.text.append(stripped)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_compile(
    source: Path,
    config: Path,
    output_dir: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "repurposer",
            "compile",
            str(source),
            "--config",
            str(config),
            "--output-dir",
            str(output_dir),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_source_text_in_collector(
    collector: TextCollector,
    source: dict[str, object],
) -> None:
    text = collector.text
    require(source["title"] in text, "newsletter title was changed or omitted")
    require(source["summary"] in text, "newsletter summary was changed or omitted")
    require(source["author"] in text, "newsletter author was changed or omitted")
    for section in source["sections"]:
        require(section["heading"] in text, "newsletter heading was changed or omitted")
        for paragraph in section["paragraphs"]:
            require(paragraph in text, "newsletter paragraph was changed or omitted")
        for bullet in section["bullets"]:
            require(bullet in text, "newsletter bullet was changed or omitted")
    action = source["call_to_action"]
    require(action["label"] in text, "newsletter action label was changed or omitted")
    require(action["url"] in collector.links, "newsletter action URL is absent")


def verify_valid_outputs(output_dir: Path, source: dict[str, object]) -> None:
    expected_files = {
        "newsletter.html",
        "feed.xml",
        "podcast-script.txt",
        "vertical-storyboard.json",
        "carousel.json",
        "plain.txt",
        "manifest.json",
    }
    actual_files = {
        path.name for path in output_dir.iterdir() if path.is_file()
    }
    require(actual_files == expected_files, f"unexpected output set: {actual_files}")

    newsletter_parser = TextCollector()
    newsletter_parser.feed((output_dir / "newsletter.html").read_text())
    assert_source_text_in_collector(newsletter_parser, source)

    rss_root = ET.parse(output_dir / "feed.xml").getroot()
    require(rss_root.tag == "rss", "RSS root is invalid")
    channel = rss_root.find("channel")
    require(channel is not None, "RSS channel is absent")
    require(channel.findtext("title") == source["title"], "RSS title changed")
    require(channel.findtext("link") == source["canonical_url"], "RSS URL changed")
    require(
        channel.findtext("description") == source["summary"],
        "RSS summary changed",
    )
    item = channel.find("item")
    require(item is not None, "RSS item is absent")
    require(
        item.findtext("{http://purl.org/dc/elements/1.1/}creator")
        == source["author"],
        "RSS creator changed",
    )
    require(
        parsedate_to_datetime(item.findtext("pubDate") or "").isoformat()
        == source["published"],
        "RSS publication date changed",
    )
    rss_description = item.findtext("description") or ""
    rss_parser = TextCollector()
    rss_parser.feed(rss_description)
    require(source["summary"] in rss_parser.text, "RSS item summary is absent")
    for section in source["sections"]:
        require(section["heading"] in rss_parser.text, "RSS section is absent")
    require(
        source["call_to_action"]["url"] in rss_parser.links,
        "RSS action URL is absent",
    )

    podcast = (output_dir / "podcast-script.txt").read_text()
    require(source["title"] in podcast, "podcast title is absent")
    require(source["summary"] in podcast, "podcast summary is absent")
    for section in source["sections"]:
        require(section["heading"] in podcast, "podcast heading is absent")
        for paragraph in section["paragraphs"]:
            require(paragraph in podcast, "podcast paragraph is absent")
        for bullet in section["bullets"]:
            require(bullet not in podcast, "podcast drop_bullets transform failed")

    storyboard = json.loads(
        (output_dir / "vertical-storyboard.json").read_text()
    )
    require(storyboard["aspect_ratio"] == "9:16", "storyboard aspect ratio changed")
    require(storyboard["total_duration_seconds"] == 24, "storyboard duration is wrong")
    require(len(storyboard["scenes"]) == 4, "storyboard scene count is wrong")
    require(
        storyboard["scenes"][0]["voiceover"] == source["summary"],
        "storyboard cover copy was synthesized",
    )
    for index, section in enumerate(source["sections"][:2], start=1):
        expected_voiceover = (
            section["paragraphs"][0] + "\n" + section["bullets"][0]
        )
        scene = storyboard["scenes"][index]
        require(
            scene["on_screen_text"] == section["heading"],
            "storyboard heading was synthesized",
        )
        require(
            scene["voiceover"] == expected_voiceover,
            "storyboard transform did not select exact source copy",
        )
    require(
        source["sections"][2]["heading"]
        not in json.dumps(storyboard, ensure_ascii=False),
        "storyboard take_sections transform failed",
    )

    carousel = json.loads((output_dir / "carousel.json").read_text())
    require(len(carousel["slides"]) == 5, "carousel slide count is wrong")
    require(
        carousel["slides"][0]["body"] == [source["summary"]],
        "carousel cover summary was synthesized",
    )
    for index, section in enumerate(source["sections"], start=1):
        slide = carousel["slides"][index]
        require(slide["heading"] == section["heading"], "carousel heading changed")
        require(
            slide["body"]
            == [section["paragraphs"][0], *section["bullets"][:2]],
            "carousel transform selected the wrong source copy",
        )

    plain = (output_dir / "plain.txt").read_text()
    for value in (source["title"], source["summary"], source["author"]):
        require(value in plain, "plain-text metadata is absent")
    for section in source["sections"]:
        require(section["heading"] in plain, "plain-text heading is absent")
        for paragraph in section["paragraphs"]:
            require(paragraph in plain, "plain-text paragraph is absent")
        for bullet in section["bullets"]:
            require(bullet in plain, "plain-text bullet is absent")

    manifest = json.loads((output_dir / "manifest.json").read_text())
    require(manifest["schema_version"] == 1, "manifest version is wrong")
    require(len(manifest["outputs"]) == 6, "manifest output count is wrong")
    require(
        {entry["format"] for entry in manifest["outputs"]}
        == {
            "newsletter_html",
            "rss",
            "podcast_script",
            "vertical_video_storyboard",
            "carousel",
            "plain_text",
        },
        "manifest format set is wrong",
    )
    for entry in manifest["outputs"]:
        generated = (output_dir / entry["filename"]).read_bytes()
        require(
            hashlib.sha256(generated).hexdigest() == entry["sha256"],
            f"manifest hash mismatch for {entry['filename']}",
        )


def main() -> int:
    source = json.loads(SOURCE_PATH.read_text())["document"]
    with tempfile.TemporaryDirectory(prefix="repurposer-verify-") as temporary:
        root = Path(temporary)
        first_output = root / "first"
        completed = run_compile(SOURCE_PATH, CONFIG_PATH, first_output)
        require(completed.returncode == 0, completed.stderr)
        require("compiled 6 formats" in completed.stdout, "CLI summary is wrong")
        verify_valid_outputs(first_output, source)
        print("PASS: CLI generated six structurally valid, source-grounded formats")

        second_output = root / "second"
        repeated = run_compile(SOURCE_PATH, CONFIG_PATH, second_output)
        require(repeated.returncode == 0, repeated.stderr)
        first_bytes = {
            path.name: path.read_bytes()
            for path in first_output.iterdir()
            if path.is_file()
        }
        second_bytes = {
            path.name: path.read_bytes()
            for path in second_output.iterdir()
            if path.is_file()
        }
        require(first_bytes == second_bytes, "repeated compile is not deterministic")
        print("PASS: repeated compilation was byte-for-byte deterministic")

        renamed_config = json.loads(CONFIG_PATH.read_text())
        renamed_config["formats"]["newsletter_html"]["output"] = (
            "newsletter-renamed.html"
        )
        renamed_config_path = root / "renamed-config.json"
        renamed_config_path.write_text(json.dumps(renamed_config))
        renamed = run_compile(SOURCE_PATH, renamed_config_path, first_output)
        require(renamed.returncode == 0, renamed.stderr)
        require(
            not (first_output / "newsletter.html").exists(),
            "manifest-owned stale output was retained",
        )
        require(
            (first_output / "newsletter-renamed.html").exists(),
            "renamed output is absent",
        )
        print("PASS: safe stale-output cleanup followed the manifest")

        before_failure = {
            path.name: path.read_bytes()
            for path in first_output.iterdir()
            if path.is_file()
        }
        invalid_source = json.loads(SOURCE_PATH.read_text())
        del invalid_source["document"]["summary"]
        invalid_source_path = root / "missing-summary.json"
        invalid_source_path.write_text(json.dumps(invalid_source))
        failed = run_compile(invalid_source_path, renamed_config_path, first_output)
        require(failed.returncode == 2, "missing source did not return exit code 2")
        require("SOURCE_GAP" in failed.stderr, "missing source error code is absent")
        require(
            "document.summary" in failed.stderr,
            "missing source path is absent from the error",
        )
        after_failure = {
            path.name: path.read_bytes()
            for path in first_output.iterdir()
            if path.is_file()
        }
        require(
            before_failure == after_failure,
            "failed compilation changed existing outputs",
        )
        print("PASS: absent source facts failed loudly before output changes")

    print("VERIFY PASS: all required behaviors observed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        print(f"VERIFY FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
