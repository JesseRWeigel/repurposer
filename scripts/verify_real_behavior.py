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
        self.ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        if tag == "a":
            attributes = dict(attrs)
            if attributes.get("href"):
                self.links.append(attributes["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped and not self.ignored_depth:
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
    expected_text = [
        source["title"],
        source["title"],
        source["author"],
        source["summary"],
    ]
    for section in source["sections"]:
        expected_text.append(section["heading"])
        expected_text.extend(section["paragraphs"])
        expected_text.extend(section["bullets"])
    action = source["call_to_action"]
    expected_text.append(action["label"])
    require(
        collector.text == expected_text,
        "newsletter visible copy differs from exact transformed source copy",
    )
    require(
        collector.links == [action["url"]],
        "newsletter links differ from exact source URLs",
    )


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
    expected_rss_text = [source["summary"]]
    for section in source["sections"]:
        expected_rss_text.append(section["heading"])
        expected_rss_text.extend(section["paragraphs"])
        expected_rss_text.extend(section["bullets"])
    expected_rss_text.append(source["call_to_action"]["label"])
    require(
        rss_parser.text == expected_rss_text,
        "RSS item copy differs from exact transformed source copy",
    )
    require(
        rss_parser.links == [source["call_to_action"]["url"]],
        "RSS item links differ from exact source URLs",
    )

    podcast = (output_dir / "podcast-script.txt").read_text()
    podcast_lines = [
        f"# {source['title']}",
        "",
        "[HOST]",
        source["summary"],
    ]
    for section in source["sections"]:
        podcast_lines.extend(["", f"## {section['heading']}"])
        for paragraph in section["paragraphs"]:
            podcast_lines.extend(["", "[HOST]", paragraph])
    action = source["call_to_action"]
    podcast_lines.extend(
        [
            "",
            "[CALL TO ACTION]",
            "[HOST]",
            f"{action['label']}: {action['url']}",
        ]
    )
    require(
        podcast == "\n".join(podcast_lines) + "\n",
        "podcast copy differs from exact transformed source copy",
    )

    storyboard = json.loads(
        (output_dir / "vertical-storyboard.json").read_text()
    )
    direction = "Use typography and visuals supported by the source."
    expected_scenes = [
        {
            "scene": 1,
            "duration_seconds": 6,
            "on_screen_text": source["title"],
            "voiceover": source["summary"],
            "visual_direction": direction,
            "source_roles": ["title", "summary"],
        }
    ]
    for section in source["sections"][:2]:
        expected_scenes.append(
            {
                "scene": len(expected_scenes) + 1,
                "duration_seconds": 6,
                "on_screen_text": section["heading"],
                "voiceover": (
                    section["paragraphs"][0] + "\n" + section["bullets"][0]
                ),
                "visual_direction": direction,
                "source_roles": ["sections"],
            }
        )
    expected_scenes.append(
        {
            "scene": 4,
            "duration_seconds": 6,
            "on_screen_text": action["label"],
            "voiceover": action["url"],
            "visual_direction": direction,
            "source_roles": ["call_to_action"],
        }
    )
    expected_storyboard = {
        "format": "vertical_video_storyboard",
        "aspect_ratio": "9:16",
        "title": source["title"],
        "total_duration_seconds": 24,
        "scenes": expected_scenes,
    }
    require(
        storyboard == expected_storyboard,
        "storyboard differs from the exact expected grounded payload",
    )

    carousel = json.loads((output_dir / "carousel.json").read_text())
    expected_slides = [
        {
            "slide": 1,
            "kind": "cover",
            "label": "Cover",
            "heading": source["title"],
            "body": [source["summary"]],
            "source_roles": ["title", "summary"],
        }
    ]
    for section in source["sections"]:
        expected_slides.append(
            {
                "slide": len(expected_slides) + 1,
                "kind": "content",
                "heading": section["heading"],
                "body": [section["paragraphs"][0], *section["bullets"][:2]],
                "source_roles": ["sections"],
            }
        )
    expected_slides.append(
        {
            "slide": 5,
            "kind": "call_to_action",
            "heading": action["label"],
            "body": [action["url"]],
            "source_roles": ["call_to_action"],
        }
    )
    require(
        carousel
        == {
            "format": "carousel",
            "title": source["title"],
            "slides": expected_slides,
        },
        "carousel differs from the exact expected grounded payload",
    )

    plain = (output_dir / "plain.txt").read_text()
    plain_lines = [
        source["title"],
        "=" * len(source["title"]),
        f"By {source['author']}",
        "",
        source["summary"],
    ]
    for section in source["sections"]:
        plain_lines.extend(
            ["", section["heading"], "-" * len(section["heading"]), ""]
        )
        plain_lines.extend(section["paragraphs"])
        plain_lines.append("")
        plain_lines.extend(f"* {bullet}" for bullet in section["bullets"])
    plain_lines.extend(["", f"{action['label']}: {action['url']}"])
    require(
        plain == "\n".join(plain_lines) + "\n",
        "plain text differs from exact transformed source copy",
    )

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

        injected_config = json.loads(CONFIG_PATH.read_text())
        injected_config["formats"]["vertical_video_storyboard"]["options"][
            "visual_direction"
        ] = "Claim a result that the source never states."
        injected_config["formats"]["carousel"]["options"]["cover_label"] = (
            "Invented factual label"
        )
        injected_config_path = root / "injected-config.json"
        injected_config_path.write_text(json.dumps(injected_config))
        injection_output = root / "injection-output"
        injection = run_compile(SOURCE_PATH, injected_config_path, injection_output)
        require(injection.returncode == 2, "free-form config copy was accepted")
        require(
            "INVALID_CONFIG" in injection.stderr
            and "unsupported options" in injection.stderr,
            "free-form config copy returned the wrong error",
        )
        require(
            not injection_output.exists(),
            "rejected free-form config wrote output files",
        )

        adversarial_sources: list[tuple[str, dict[str, object], str]] = []

        malformed_url = json.loads(SOURCE_PATH.read_text())
        malformed_url["document"]["canonical_url"] = (
            "https://exa mple.com/not-valid"
        )
        adversarial_sources.append(
            ("malformed-url", malformed_url, "INVALID_SOURCE")
        )

        invalid_xml = json.loads(SOURCE_PATH.read_text())
        invalid_xml["document"]["summary"] += "\u0001"
        adversarial_sources.append(
            ("invalid-xml", invalid_xml, "INVALID_SOURCE")
        )

        emptied_section = json.loads(SOURCE_PATH.read_text())
        emptied_section["document"]["sections"][0] = {
            "heading": "Bullet-only section",
            "paragraphs": [],
            "bullets": ["Content that drop_bullets would remove."],
        }
        adversarial_sources.append(
            ("empty-transform", emptied_section, "EMPTY_FORMAT")
        )

        for name, payload, error_code in adversarial_sources:
            adversarial_path = root / f"{name}.json"
            adversarial_path.write_text(json.dumps(payload))
            adversarial_output = root / f"{name}-output"
            result = run_compile(
                adversarial_path,
                CONFIG_PATH,
                adversarial_output,
            )
            require(result.returncode == 2, f"{name} input was accepted")
            require(
                error_code in result.stderr,
                f"{name} returned the wrong error code",
            )
            require(
                not adversarial_output.exists(),
                f"{name} failure wrote output files",
            )
        print("PASS: adversarial config, URLs, XML text, and transforms were rejected")

    print("VERIFY PASS: all required behaviors observed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        print(f"VERIFY FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
