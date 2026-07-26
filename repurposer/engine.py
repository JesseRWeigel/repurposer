"""Strict, deterministic compilation engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable
from urllib.parse import urlparse
import xml.etree.ElementTree as ET


FORMAT_ORDER = (
    "newsletter_html",
    "rss",
    "podcast_script",
    "vertical_video_storyboard",
    "carousel",
    "plain_text",
)

ROLE_TYPES: dict[str, type] = {
    "title": str,
    "summary": str,
    "author": str,
    "published": str,
    "canonical_url": str,
    "language": str,
    "sections": list,
    "call_to_action": dict,
}

REQUIRED_ROLES: dict[str, frozenset[str]] = {
    "newsletter_html": frozenset(
        {"title", "summary", "author", "language", "sections"}
    ),
    "rss": frozenset(
        {
            "title",
            "summary",
            "author",
            "published",
            "canonical_url",
            "sections",
        }
    ),
    "podcast_script": frozenset({"title", "summary", "sections"}),
    "vertical_video_storyboard": frozenset({"title", "summary", "sections"}),
    "carousel": frozenset({"title", "summary", "sections"}),
    "plain_text": frozenset({"title", "summary", "author", "sections"}),
}

OPTION_KEYS: dict[str, frozenset[str]] = {
    "newsletter_html": frozenset({"accent_color"}),
    "rss": frozenset(),
    "podcast_script": frozenset({"speaker"}),
    "vertical_video_storyboard": frozenset({"seconds_per_scene"}),
    "carousel": frozenset(),
    "plain_text": frozenset({"include_urls"}),
}

OUTPUT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
PATH_PART_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"


class RepurposerError(Exception):
    """An expected input or compilation error."""

    def __init__(self, message: str, code: str = "COMPILE_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompileResult:
    """Summary of one completed compilation."""

    output_dir: Path
    outputs: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...] = ()


@dataclass
class FormatView:
    """Resolved source roles for a single format."""

    roles: dict[str, Any]
    source_paths: dict[str, str]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RepurposerError(
                f'duplicate JSON key "{key}"',
                code="INVALID_JSON",
            )
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RepurposerError(
            f"could not read {label} {path}: {exc}",
            code="IO_ERROR",
        ) from exc
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise RepurposerError(
            f"{label} is not valid UTF-8: {path}",
            code="INVALID_JSON",
        ) from exc
    except json.JSONDecodeError as exc:
        raise RepurposerError(
            f"{label} JSON error at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            code="INVALID_JSON",
        ) from exc
    if not isinstance(value, dict):
        raise RepurposerError(
            f"{label} root must be an object",
            code="INVALID_SCHEMA",
        )
    return value, raw


def _nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RepurposerError(
            f"{context} must be a non-empty string",
            code="INVALID_SOURCE",
        )
    invalid_codepoint = next(
        (
            ord(character)
            for character in value
            if not (
                character in "\t\n\r"
                or "\u0020" <= character <= "\ud7ff"
                or "\ue000" <= character <= "\ufffd"
                or "\U00010000" <= character <= "\U0010ffff"
            )
        ),
        None,
    )
    if invalid_codepoint is not None:
        raise RepurposerError(
            f"{context} contains invalid Unicode code point U+{invalid_codepoint:04X}",
            code="INVALID_SOURCE",
        )
    return value


def _resolve_path(source: dict[str, Any], source_path: str, format_name: str) -> Any:
    if not isinstance(source_path, str) or not source_path:
        raise RepurposerError(
            f'[{format_name}] source paths must be non-empty strings',
            code="INVALID_CONFIG",
        )
    parts = source_path.split(".")
    if any(not PATH_PART_RE.fullmatch(part) for part in parts):
        raise RepurposerError(
            f'[{format_name}] invalid source path "{source_path}"',
            code="INVALID_CONFIG",
        )

    current: Any = source
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise RepurposerError(
                f'[{format_name}] missing required source path "{source_path}"',
                code="SOURCE_GAP",
            )
        current = current[part]
    if current is None:
        raise RepurposerError(
            f'[{format_name}] source path "{source_path}" is null',
            code="SOURCE_GAP",
        )
    return current


def _validate_url(value: str, context: str) -> None:
    if (
        any(character.isspace() or ord(character) < 0x20 for character in value)
        or "\\" in value
        or BAD_PERCENT_RE.search(value)
    ):
        raise RepurposerError(
            f"{context} contains invalid URL characters",
            code="INVALID_SOURCE",
        )
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RepurposerError(
            f"{context} is not a valid URL",
            code="INVALID_SOURCE",
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RepurposerError(
            f"{context} must be an absolute HTTP or HTTPS URL",
            code="INVALID_SOURCE",
        )
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise RepurposerError(
                f"{context} has an invalid hostname",
                code="INVALID_SOURCE",
            ) from exc
        if len(ascii_hostname) > 253 or any(
            not HOST_LABEL_RE.fullmatch(label)
            for label in ascii_hostname.rstrip(".").split(".")
        ):
            raise RepurposerError(
                f"{context} has an invalid hostname",
                code="INVALID_SOURCE",
            )


def _validate_sections(value: Any, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise RepurposerError(
            f"{context} must be a non-empty array",
            code="INVALID_SOURCE",
        )
    validated: list[dict[str, Any]] = []
    for index, raw_section in enumerate(value):
        prefix = f"{context}[{index}]"
        if not isinstance(raw_section, dict):
            raise RepurposerError(
                f"{prefix} must be an object",
                code="INVALID_SOURCE",
            )
        unknown = set(raw_section) - {"heading", "paragraphs", "bullets"}
        if unknown:
            raise RepurposerError(
                f"{prefix} has unsupported keys: {', '.join(sorted(unknown))}",
                code="INVALID_SOURCE",
            )
        heading = _nonempty_string(raw_section.get("heading"), f"{prefix}.heading")
        paragraphs = raw_section.get("paragraphs", [])
        bullets = raw_section.get("bullets", [])
        if not isinstance(paragraphs, list):
            raise RepurposerError(
                f"{prefix}.paragraphs must be an array of non-empty strings",
                code="INVALID_SOURCE",
            )
        if not isinstance(bullets, list):
            raise RepurposerError(
                f"{prefix}.bullets must be an array of non-empty strings",
                code="INVALID_SOURCE",
            )
        validated_paragraphs = [
            _nonempty_string(item, f"{prefix}.paragraphs[{item_index}]")
            for item_index, item in enumerate(paragraphs)
        ]
        validated_bullets = [
            _nonempty_string(item, f"{prefix}.bullets[{item_index}]")
            for item_index, item in enumerate(bullets)
        ]
        if not paragraphs and not bullets:
            raise RepurposerError(
                f"{prefix} needs at least one paragraph or bullet",
                code="INVALID_SOURCE",
            )
        validated.append(
            {
                "heading": heading,
                "paragraphs": validated_paragraphs,
                "bullets": validated_bullets,
            }
        )
    return validated


def _validate_role(role: str, value: Any, format_name: str, source_path: str) -> Any:
    expected = ROLE_TYPES[role]
    context = f'[{format_name}] source path "{source_path}"'
    if not isinstance(value, expected):
        raise RepurposerError(
            f"{context} must resolve to {expected.__name__}",
            code="INVALID_SOURCE",
        )
    if expected is str:
        value = _nonempty_string(value, context)
    if role in {"canonical_url"}:
        _validate_url(value, context)
    if role == "sections":
        return _validate_sections(value, context)
    if role == "call_to_action":
        unknown = set(value) - {"label", "url"}
        if unknown:
            raise RepurposerError(
                f"{context} has unsupported keys: {', '.join(sorted(unknown))}",
                code="INVALID_SOURCE",
            )
        label = _nonempty_string(value.get("label"), f"{context}.label")
        url = _nonempty_string(value.get("url"), f"{context}.url")
        _validate_url(url, f"{context}.url")
        return {"label": label, "url": url}
    return value


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RepurposerError(
            f"{context} must be a positive integer",
            code="INVALID_CONFIG",
        )
    return value


def _validate_options(format_name: str, raw_options: Any) -> dict[str, Any]:
    if raw_options is None:
        return {}
    if not isinstance(raw_options, dict):
        raise RepurposerError(
            f"[{format_name}] options must be an object",
            code="INVALID_CONFIG",
        )
    unknown = set(raw_options) - OPTION_KEYS[format_name]
    if unknown:
        raise RepurposerError(
            f"[{format_name}] unsupported options: {', '.join(sorted(unknown))}",
            code="INVALID_CONFIG",
        )
    options = dict(raw_options)
    if "accent_color" in options:
        if not isinstance(options["accent_color"], str) or not HEX_COLOR_RE.fullmatch(
            options["accent_color"]
        ):
            raise RepurposerError(
                f"[{format_name}] accent_color must be a six-digit hex color",
                code="INVALID_CONFIG",
            )
    if "speaker" in options:
        if (
            not isinstance(options["speaker"], str)
            or options["speaker"] not in {"HOST", "NARRATOR"}
        ):
            raise RepurposerError(
                f"[{format_name}] speaker must be HOST or NARRATOR",
                code="INVALID_CONFIG",
            )
    if "seconds_per_scene" in options:
        options["seconds_per_scene"] = _positive_int(
            options["seconds_per_scene"],
            f"[{format_name}] seconds_per_scene",
        )
    if "include_urls" in options and not isinstance(options["include_urls"], bool):
        raise RepurposerError(
            f"[{format_name}] include_urls must be a boolean",
            code="INVALID_CONFIG",
        )
    return options


def _apply_transforms(
    view: FormatView,
    transforms: Any,
    format_name: str,
) -> FormatView:
    if not isinstance(transforms, list):
        raise RepurposerError(
            f"[{format_name}] transforms must be an array",
            code="INVALID_CONFIG",
        )
    sections = [
        {
            "heading": section["heading"],
            "paragraphs": list(section["paragraphs"]),
            "bullets": list(section["bullets"]),
        }
        for section in view.roles["sections"]
    ]
    seen: set[str] = set()
    for index, transform in enumerate(transforms):
        context = f"[{format_name}] transforms[{index}]"
        if not isinstance(transform, dict):
            raise RepurposerError(
                f"{context} must be an object",
                code="INVALID_CONFIG",
            )
        op = transform.get("op")
        if not isinstance(op, str):
            raise RepurposerError(
                f"{context}.op must be a string",
                code="INVALID_CONFIG",
            )
        if op in seen:
            raise RepurposerError(
                f'{context} repeats operation "{op}"',
                code="INVALID_CONFIG",
            )
        seen.add(op)
        if op == "take_sections":
            if set(transform) != {"op", "count"}:
                raise RepurposerError(
                    f"{context} requires only op and count",
                    code="INVALID_CONFIG",
                )
            sections = sections[: _positive_int(transform["count"], f"{context}.count")]
        elif op == "take_paragraphs_per_section":
            if set(transform) != {"op", "count"}:
                raise RepurposerError(
                    f"{context} requires only op and count",
                    code="INVALID_CONFIG",
                )
            count = _positive_int(transform["count"], f"{context}.count")
            for section in sections:
                section["paragraphs"] = section["paragraphs"][:count]
        elif op == "take_bullets_per_section":
            if set(transform) != {"op", "count"}:
                raise RepurposerError(
                    f"{context} requires only op and count",
                    code="INVALID_CONFIG",
                )
            count = _positive_int(transform["count"], f"{context}.count")
            for section in sections:
                section["bullets"] = section["bullets"][:count]
        elif op == "drop_bullets":
            if set(transform) != {"op"}:
                raise RepurposerError(
                    f"{context} accepts only op",
                    code="INVALID_CONFIG",
                )
            for section in sections:
                section["bullets"] = []
        else:
            raise RepurposerError(
                f'{context} uses unsupported operation "{op}"',
                code="INVALID_CONFIG",
            )
    if not sections:
        raise RepurposerError(
            f"[{format_name}] transforms removed every section",
            code="EMPTY_FORMAT",
        )
    empty_sections = [
        section["heading"]
        for section in sections
        if not section["paragraphs"] and not section["bullets"]
    ]
    if empty_sections:
        raise RepurposerError(
            f'[{format_name}] transforms removed all content from section '
            f'"{empty_sections[0]}"',
            code="EMPTY_FORMAT",
        )
    roles = dict(view.roles)
    roles["sections"] = sections
    return FormatView(roles=roles, source_paths=view.source_paths)


def _resolve_view(
    source: dict[str, Any],
    raw_format: Any,
    format_name: str,
) -> tuple[FormatView, str, dict[str, Any]]:
    if not isinstance(raw_format, dict):
        raise RepurposerError(
            f"[{format_name}] configuration must be an object",
            code="INVALID_CONFIG",
        )
    unknown = set(raw_format) - {"output", "source", "transforms", "options"}
    if unknown:
        raise RepurposerError(
            f"[{format_name}] unsupported keys: {', '.join(sorted(unknown))}",
            code="INVALID_CONFIG",
        )
    if "transforms" not in raw_format:
        raise RepurposerError(
            f"[{format_name}] must declare transforms, use an empty array when none apply",
            code="INVALID_CONFIG",
        )
    output = raw_format.get("output")
    if not isinstance(output, str) or not OUTPUT_RE.fullmatch(output):
        raise RepurposerError(
            f"[{format_name}] output must be a safe filename",
            code="INVALID_CONFIG",
        )
    if output == "manifest.json":
        raise RepurposerError(
            f"[{format_name}] output cannot be manifest.json",
            code="INVALID_CONFIG",
        )
    mappings = raw_format.get("source")
    if not isinstance(mappings, dict):
        raise RepurposerError(
            f"[{format_name}] source must be an object",
            code="INVALID_CONFIG",
        )
    unknown_roles = set(mappings) - set(ROLE_TYPES)
    if unknown_roles:
        raise RepurposerError(
            f"[{format_name}] unknown source roles: {', '.join(sorted(unknown_roles))}",
            code="INVALID_CONFIG",
        )
    missing_roles = REQUIRED_ROLES[format_name] - set(mappings)
    if missing_roles:
        raise RepurposerError(
            f"[{format_name}] missing source role mappings: "
            + ", ".join(sorted(missing_roles)),
            code="INVALID_CONFIG",
        )
    roles: dict[str, Any] = {}
    source_paths: dict[str, str] = {}
    for role, source_path in mappings.items():
        value = _resolve_path(source, source_path, format_name)
        roles[role] = _validate_role(role, value, format_name, source_path)
        source_paths[role] = source_path
    view = _apply_transforms(
        FormatView(roles=roles, source_paths=source_paths),
        raw_format["transforms"],
        format_name,
    )
    options = _validate_options(format_name, raw_format.get("options"))
    return view, output, options


def _section_html(sections: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for section in sections:
        chunks.append(f"<section><h2>{html.escape(section['heading'])}</h2>")
        chunks.extend(f"<p>{html.escape(text)}</p>" for text in section["paragraphs"])
        if section["bullets"]:
            chunks.append("<ul>")
            chunks.extend(
                f"<li>{html.escape(text)}</li>" for text in section["bullets"]
            )
            chunks.append("</ul>")
        chunks.append("</section>")
    return "".join(chunks)


def _render_newsletter(view: FormatView, options: dict[str, Any]) -> str:
    roles = view.roles
    accent = options.get("accent_color", "#335CFF")
    language = html.escape(roles["language"], quote=True)
    cta = ""
    if "call_to_action" in roles:
        action = roles["call_to_action"]
        cta = (
            '<p class="cta"><a href="'
            + html.escape(action["url"], quote=True)
            + '">'
            + html.escape(action["label"])
            + "</a></p>"
        )
    return (
        "<!doctype html>\n"
        f'<html lang="{language}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(roles['title'])}</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;line-height:1.6;color:#172033;"
        "max-width:680px;margin:0 auto;padding:32px 20px}"
        f"h1,h2,a{{color:{accent}}}"
        ".byline{color:#526070}.summary{font-size:1.15rem}"
        ".cta a{display:inline-block;padding:10px 16px;border:2px solid currentColor;"
        "border-radius:6px;font-weight:700}"
        "</style></head><body>"
        f"<main><h1>{html.escape(roles['title'])}</h1>"
        f'<p class="byline">{html.escape(roles["author"])}</p>'
        f'<p class="summary">{html.escape(roles["summary"])}</p>'
        f"{_section_html(roles['sections'])}{cta}</main></body></html>\n"
    )


def _render_rss(view: FormatView, options: dict[str, Any]) -> str:
    del options
    roles = view.roles
    try:
        published = datetime.fromisoformat(
            roles["published"].replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise RepurposerError(
            '[rss] published source must use ISO 8601 format',
            code="INVALID_SOURCE",
        ) from exc
    if published.tzinfo is None:
        raise RepurposerError(
            "[rss] published source must include a timezone",
            code="INVALID_SOURCE",
        )

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = roles["title"]
    ET.SubElement(channel, "link").text = roles["canonical_url"]
    ET.SubElement(channel, "description").text = roles["summary"]
    if "language" in roles:
        ET.SubElement(channel, "language").text = roles["language"]
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = roles["title"]
    ET.SubElement(item, "link").text = roles["canonical_url"]
    ET.SubElement(item, "guid", isPermaLink="true").text = roles["canonical_url"]
    ET.register_namespace("dc", DC_NAMESPACE)
    ET.SubElement(item, f"{{{DC_NAMESPACE}}}creator").text = roles["author"]
    ET.SubElement(item, "pubDate").text = format_datetime(published)
    description = (
        f"<p>{html.escape(roles['summary'])}</p>"
        + _section_html(roles["sections"])
    )
    if "call_to_action" in roles:
        action = roles["call_to_action"]
        description += (
            '<p><a href="'
            + html.escape(action["url"], quote=True)
            + '">'
            + html.escape(action["label"])
            + "</a></p>"
        )
    ET.SubElement(item, "description").text = description
    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
        rss,
        encoding="unicode",
        short_empty_elements=True,
    ) + "\n"


def _render_podcast(view: FormatView, options: dict[str, Any]) -> str:
    roles = view.roles
    speaker = options.get("speaker", "HOST")
    lines = [
        f"# {roles['title']}",
        "",
        f"[{speaker}]",
        roles["summary"],
    ]
    for section in roles["sections"]:
        lines.extend(["", f"## {section['heading']}"])
        for paragraph in section["paragraphs"]:
            lines.extend(["", f"[{speaker}]", paragraph])
        for bullet in section["bullets"]:
            lines.extend(["", f"[{speaker}]", bullet])
    if "call_to_action" in roles:
        action = roles["call_to_action"]
        lines.extend(
            [
                "",
                "[CALL TO ACTION]",
                f"[{speaker}]",
                f"{action['label']}: {action['url']}",
            ]
        )
    return "\n".join(lines) + "\n"


def _render_storyboard(view: FormatView, options: dict[str, Any]) -> str:
    roles = view.roles
    seconds = options.get("seconds_per_scene", 7)
    direction = "Use typography and visuals supported by the source."
    scenes = [
        {
            "scene": 1,
            "duration_seconds": seconds,
            "on_screen_text": roles["title"],
            "voiceover": roles["summary"],
            "visual_direction": direction,
            "source_roles": ["title", "summary"],
        }
    ]
    for index, section in enumerate(roles["sections"], start=2):
        voiceover_parts = section["paragraphs"] + section["bullets"]
        scenes.append(
            {
                "scene": index,
                "duration_seconds": seconds,
                "on_screen_text": section["heading"],
                "voiceover": "\n".join(voiceover_parts),
                "visual_direction": direction,
                "source_roles": ["sections"],
            }
        )
    if "call_to_action" in roles:
        action = roles["call_to_action"]
        scenes.append(
            {
                "scene": len(scenes) + 1,
                "duration_seconds": seconds,
                "on_screen_text": action["label"],
                "voiceover": action["url"],
                "visual_direction": direction,
                "source_roles": ["call_to_action"],
            }
        )
    payload = {
        "format": "vertical_video_storyboard",
        "aspect_ratio": "9:16",
        "title": roles["title"],
        "total_duration_seconds": len(scenes) * seconds,
        "scenes": scenes,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _render_carousel(view: FormatView, options: dict[str, Any]) -> str:
    roles = view.roles
    slides = [
        {
            "slide": 1,
            "kind": "cover",
            "label": "Cover",
            "heading": roles["title"],
            "body": [roles["summary"]],
            "source_roles": ["title", "summary"],
        }
    ]
    for section in roles["sections"]:
        slides.append(
            {
                "slide": len(slides) + 1,
                "kind": "content",
                "heading": section["heading"],
                "body": section["paragraphs"] + section["bullets"],
                "source_roles": ["sections"],
            }
        )
    if "call_to_action" in roles:
        action = roles["call_to_action"]
        slides.append(
            {
                "slide": len(slides) + 1,
                "kind": "call_to_action",
                "heading": action["label"],
                "body": [action["url"]],
                "source_roles": ["call_to_action"],
            }
        )
    payload = {
        "format": "carousel",
        "title": roles["title"],
        "slides": slides,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _render_plain_text(view: FormatView, options: dict[str, Any]) -> str:
    roles = view.roles
    lines = [
        roles["title"],
        "=" * len(roles["title"]),
        f"By {roles['author']}",
        "",
        roles["summary"],
    ]
    for section in roles["sections"]:
        lines.extend(["", section["heading"], "-" * len(section["heading"])])
        lines.extend(["", *section["paragraphs"]])
        if section["bullets"]:
            lines.extend(["", *(f"* {bullet}" for bullet in section["bullets"])])
    if "call_to_action" in roles:
        action = roles["call_to_action"]
        text = action["label"]
        if options.get("include_urls", True):
            text += f": {action['url']}"
        lines.extend(["", text])
    return "\n".join(lines) + "\n"


RENDERERS: dict[str, Callable[[FormatView, dict[str, Any]], str]] = {
    "newsletter_html": _render_newsletter,
    "rss": _render_rss,
    "podcast_script": _render_podcast,
    "vertical_video_storyboard": _render_storyboard,
    "carousel": _render_carousel,
    "plain_text": _render_plain_text,
}


def _validate_config_root(config: dict[str, Any]) -> dict[str, Any]:
    unknown = set(config) - {"version", "formats"}
    if unknown:
        raise RepurposerError(
            f"configuration has unsupported keys: {', '.join(sorted(unknown))}",
            code="INVALID_CONFIG",
        )
    if config.get("version") != 1:
        raise RepurposerError(
            "configuration version must be 1",
            code="INVALID_CONFIG",
        )
    formats = config.get("formats")
    if not isinstance(formats, dict):
        raise RepurposerError(
            "configuration formats must be an object",
            code="INVALID_CONFIG",
        )
    actual = set(formats)
    expected = set(FORMAT_ORDER)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unsupported " + ", ".join(extra))
        raise RepurposerError(
            "configuration must define all six formats: " + "; ".join(details),
            code="INVALID_CONFIG",
        )
    return formats


def _read_previous_manifest(output_dir: Path) -> dict[str, str]:
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_symlink():
        raise RepurposerError(
            "existing manifest cannot be a symbolic link",
            code="OUTPUT_CONFLICT",
        )
    if not manifest_path.exists():
        return {}
    if not manifest_path.is_file():
        raise RepurposerError(
            "existing manifest must be a regular file",
            code="OUTPUT_CONFLICT",
        )
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except RepurposerError as exc:
        raise RepurposerError(
            f"existing manifest is invalid: {exc}",
            code="OUTPUT_CONFLICT",
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepurposerError(
            f"existing manifest is unreadable: {exc}",
            code="OUTPUT_CONFLICT",
        ) from exc
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise RepurposerError(
            "existing manifest is not owned by this compiler version",
            code="OUTPUT_CONFLICT",
        )
    raw_outputs = manifest.get("outputs")
    if not isinstance(raw_outputs, list):
        raise RepurposerError(
            "existing manifest outputs must be an array",
            code="OUTPUT_CONFLICT",
        )
    previous: dict[str, str] = {}
    for entry in raw_outputs:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("filename"), str)
            or not OUTPUT_RE.fullmatch(entry["filename"])
            or not isinstance(entry.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise RepurposerError(
                "existing manifest has an invalid output entry",
                code="OUTPUT_CONFLICT",
            )
        if entry["filename"] in previous:
            raise RepurposerError(
                "existing manifest repeats an output filename",
                code="OUTPUT_CONFLICT",
            )
        previous[entry["filename"]] = entry["sha256"]
    return previous


def _existing_regular_file(path: Path, label: str) -> bool:
    if path.is_symlink():
        raise RepurposerError(
            f'{label} "{path.name}" cannot be a symbolic link',
            code="OUTPUT_CONFLICT",
        )
    if not path.exists():
        return False
    if not path.is_file():
        raise RepurposerError(
            f'{label} "{path.name}" must be a regular file',
            code="OUTPUT_CONFLICT",
        )
    return True


def _make_backup_path(output_dir: Path, filename: str) -> Path:
    handle, backup_name = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".backup",
        dir=output_dir,
    )
    os.close(handle)
    backup = Path(backup_name)
    backup.unlink()
    return backup


def _write_outputs(
    output_dir: Path,
    rendered: dict[str, bytes],
    manifest_bytes: bytes,
) -> tuple[str, ...]:
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = _read_previous_manifest(output_dir)
    stale = set(previous) - set(rendered)

    for filename in rendered:
        destination = output_dir / filename
        if _existing_regular_file(destination, "output"):
            if filename not in previous:
                raise RepurposerError(
                    f'refusing to overwrite unowned output "{filename}"',
                    code="OUTPUT_CONFLICT",
                )
            actual_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            if actual_hash != previous[filename]:
                raise RepurposerError(
                    f'refusing to overwrite modified output "{filename}"',
                    code="OUTPUT_CONFLICT",
                )

    for filename in stale:
        stale_path = output_dir / filename
        if _existing_regular_file(stale_path, "stale output"):
            actual_hash = hashlib.sha256(stale_path.read_bytes()).hexdigest()
            if actual_hash != previous[filename]:
                raise RepurposerError(
                    f'refusing to remove modified stale output "{filename}"',
                    code="OUTPUT_CONFLICT",
                )

    staged: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    committed = False
    warnings: list[str] = []
    try:
        for filename, content in {**rendered, "manifest.json": manifest_bytes}.items():
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{filename}.",
                suffix=".tmp",
                dir=output_dir,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o644)
                    os.fsync(stream.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            staged.append((temporary, output_dir / filename))

        destinations = [destination for _, destination in staged]
        destinations.extend(output_dir / filename for filename in sorted(stale))
        for destination in destinations:
            if destination.exists():
                backup = _make_backup_path(output_dir, destination.name)
                os.replace(destination, backup)
                backups.append((backup, destination))

        for temporary, destination in staged:
            os.replace(temporary, destination)
            installed.append(destination)
        committed = True
    except BaseException as exc:
        rollback_errors: list[str] = []
        for destination in reversed(installed):
            try:
                destination.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for backup, destination in reversed(backups):
            try:
                os.replace(backup, destination)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise RepurposerError(
                "output transaction failed and rollback was incomplete: "
                + "; ".join(rollback_errors),
                code="OUTPUT_ROLLBACK_FAILED",
            ) from exc
        raise
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        if committed:
            for backup, _ in backups:
                last_error: OSError | None = None
                for _ in range(3):
                    try:
                        backup.unlink(missing_ok=True)
                        last_error = None
                        break
                    except OSError as cleanup_error:
                        last_error = cleanup_error
                if last_error is not None:
                    warnings.append(
                        f'committed output, but could not remove backup '
                        f'"{backup.name}": {last_error}'
                    )
    return tuple(warnings)


def compile_document(
    source_path: Path | str,
    config_path: Path | str,
    output_dir: Path | str,
) -> CompileResult:
    """Compile a canonical source after validating every format."""

    source_path = Path(source_path)
    config_path = Path(config_path)
    output_dir = Path(output_dir)
    source, source_raw = _load_json(source_path, "source")
    config, config_raw = _load_json(config_path, "configuration")
    formats = _validate_config_root(config)

    rendered: dict[str, bytes] = {}
    output_records: list[dict[str, Any]] = []
    result_outputs: list[tuple[str, str]] = []
    seen_filenames: set[str] = set()
    for format_name in FORMAT_ORDER:
        view, filename, options = _resolve_view(
            source,
            formats[format_name],
            format_name,
        )
        if filename in seen_filenames:
            raise RepurposerError(
                f'duplicate output filename "{filename}"',
                code="INVALID_CONFIG",
            )
        seen_filenames.add(filename)
        text = RENDERERS[format_name](view, options)
        content = text.encode("utf-8")
        rendered[filename] = content
        output_records.append(
            {
                "format": format_name,
                "filename": filename,
                "sha256": hashlib.sha256(content).hexdigest(),
                "source_paths": view.source_paths,
                "transforms": formats[format_name]["transforms"],
            }
        )
        result_outputs.append((format_name, filename))

    manifest = {
        "schema_version": 1,
        "source_sha256": hashlib.sha256(source_raw).hexdigest(),
        "config_sha256": hashlib.sha256(config_raw).hexdigest(),
        "outputs": output_records,
    }
    manifest_bytes = (
        json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    )
    warnings = _write_outputs(output_dir, rendered, manifest_bytes)
    return CompileResult(
        output_dir=output_dir,
        outputs=tuple(result_outputs),
        warnings=warnings,
    )
