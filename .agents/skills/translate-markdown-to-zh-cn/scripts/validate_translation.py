#!/usr/bin/env python3
"""Validate structural fidelity between English and Simplified Chinese Markdown."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Iterable
from urllib.parse import unquote, urlsplit


FENCE_PATTERN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)$")
HEADING_PATTERN = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)\s*$")
LIST_PATTERN = re.compile(r"^[ \t]*(?:(?P<bullet>[-+*])|(?P<number>\d+[.)]))[ \t]+")
TABLE_PATTERN = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
BLOCKQUOTE_PATTERN = re.compile(r"^[ \t]{0,3}>")
INLINE_CODE_PATTERN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
INLINE_LINK_PATTERN = re.compile(
    r"(?P<image>!?)\[(?P<label>[^\]]*)]\("
    r"(?P<destination><[^>]+>|[^)\s]+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\)"
)
REFERENCE_DEFINITION_PATTERN = re.compile(
    r"^[ \t]{0,3}\[(?P<identifier>[^\]]+)]:[ \t]*"
    r"(?P<destination><[^>]+>|[^\s]+)",
    re.MULTILINE,
)
REFERENCE_USAGE_PATTERN = re.compile(
    r"(?P<image>!?)\[(?P<label>[^\]]*)]\[(?P<identifier>[^\]]*)]"
)
EXPLICIT_ID_PATTERN = re.compile(r"\s*\{#([^}\s]+)}\s*$")
HTML_ID_PATTERN = re.compile(
    r"<(?:a|span)\b[^>]*\b(?:id|name)=[\"']([^\"']+)[\"'][^>]*>",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&*+,;=%()^-]+",
    re.IGNORECASE,
)
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
ENGLISH_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9'’+._^-]*")
CODE_BLOCK_SENTINEL = "\x00FENCED_CODE_BLOCK\x00"


@dataclass(frozen=True)
class CodeBlock:
    info: str
    body: str


@dataclass(frozen=True)
class Link:
    destination: str
    is_image: bool


@dataclass
class DocumentFacts:
    masked: str
    code_blocks: list[CodeBlock]
    headings: list[tuple[int, str]]
    heading_anchors: set[str]
    inline_code: list[str]
    block_count: int
    list_types: list[str]
    table_columns: list[int]
    blockquote_lines: int
    structural_sequence: list[str]
    links: list[Link]
    bare_urls: list[str]
    untranslated_segments: list[dict[str, object]]
    parse_errors: list[str]


def _line_break(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _mask_fenced_code(text: str) -> tuple[str, list[CodeBlock], list[str]]:
    masked_lines: list[str] = []
    blocks: list[CodeBlock] = []
    errors: list[str] = []
    opening_char = ""
    opening_length = 0
    opening_info = ""
    opening_line = 0
    body: list[str] = []

    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
        logical = line.rstrip("\r\n")
        if not opening_char:
            match = FENCE_PATTERN.match(logical)
            if match:
                marker = match.group(1)
                opening_char = marker[0]
                opening_length = len(marker)
                opening_info = match.group(2).strip()
                opening_line = line_number
                body = []
                masked_lines.append(CODE_BLOCK_SENTINEL + _line_break(line))
            else:
                masked_lines.append(line)
            continue

        close = re.match(
            rf"^[ \t]{{0,3}}{re.escape(opening_char)}"
            rf"{{{opening_length},}}[ \t]*$",
            logical,
        )
        masked_lines.append(_line_break(line))
        if close:
            blocks.append(CodeBlock(info=opening_info, body="".join(body)))
            opening_char = ""
            opening_length = 0
            opening_info = ""
            opening_line = 0
            body = []
        else:
            body.append(line)

    if opening_char:
        errors.append(f"Unclosed fenced code block starting at line {opening_line}")
    return "".join(masked_lines), blocks, errors


def _normalize_destination(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1]
    return value


def _reference_definitions(text: str) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for match in REFERENCE_DEFINITION_PATTERN.finditer(text):
        identifier = " ".join(match.group("identifier").split()).casefold()
        if identifier.startswith("^"):
            continue
        definitions[identifier] = _normalize_destination(match.group("destination"))
    return definitions


def _extract_links(text: str) -> list[Link]:
    found: list[tuple[int, Link]] = []
    occupied: list[tuple[int, int]] = []
    for match in INLINE_LINK_PATTERN.finditer(text):
        found.append(
            (
                match.start(),
                Link(
                    destination=_normalize_destination(match.group("destination")),
                    is_image=bool(match.group("image")),
                ),
            )
        )
        occupied.append(match.span())

    definitions = _reference_definitions(text)
    for match in REFERENCE_USAGE_PATTERN.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        identifier = match.group("identifier") or match.group("label")
        normalized = " ".join(identifier.split()).casefold()
        destination = definitions.get(normalized)
        if destination is None:
            continue
        found.append(
            (
                match.start(),
                Link(
                    destination=destination,
                    is_image=bool(match.group("image")),
                ),
            )
        )
    return [link for _, link in sorted(found, key=lambda item: item[0])]


def _strip_inline_markup(value: str) -> str:
    value = INLINE_LINK_PATTERN.sub(lambda match: match.group("label"), value)
    value = REFERENCE_USAGE_PATTERN.sub(lambda match: match.group("label"), value)
    value = INLINE_CODE_PATTERN.sub(lambda match: match.group(2), value)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"[*_~]", "", value)
    value = re.sub(r"\\([\\`*_[\]{}()#+.!-])", r"\1", value)
    return value


def _github_slug(value: str) -> tuple[str, str | None]:
    explicit_match = EXPLICIT_ID_PATTERN.search(value)
    explicit = explicit_match.group(1) if explicit_match else None
    if explicit_match:
        value = value[: explicit_match.start()]
    value = _strip_inline_markup(value).strip().casefold()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", "-", value)
    return value.strip("-"), explicit


def _heading_facts(masked: str) -> tuple[list[tuple[int, str]], set[str]]:
    headings: list[tuple[int, str]] = []
    anchors: set[str] = set()
    slug_counts: dict[str, int] = {}
    for line in masked.splitlines():
        match = HEADING_PATTERN.match(line)
        if not match:
            continue
        title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2))
        headings.append((len(match.group(1)), title))
        slug, explicit = _github_slug(title)
        if explicit:
            anchors.add(explicit.casefold())
        if slug:
            occurrence = slug_counts.get(slug, 0)
            anchor = slug if occurrence == 0 else f"{slug}-{occurrence}"
            anchors.add(anchor)
            slug_counts[slug] = occurrence + 1
    anchors.update(match.group(1).casefold() for match in HTML_ID_PATTERN.finditer(masked))
    return headings, anchors


def _count_table_columns(line: str) -> int:
    logical = line.strip()[1:-1]
    return len(re.split(r"(?<!\\)\|", logical))


def _structural_sequence(masked: str) -> list[str]:
    sequence: list[str] = []
    for line in masked.splitlines():
        if line == CODE_BLOCK_SENTINEL:
            sequence.append("code-block")
            continue
        heading = HEADING_PATTERN.match(line)
        if heading:
            sequence.append(f"heading-{len(heading.group(1))}")
            continue
        if BLOCKQUOTE_PATTERN.match(line):
            sequence.append("blockquote")
            continue
        listed = LIST_PATTERN.match(line)
        if listed:
            sequence.append("list-bullet" if listed.group("bullet") else "list-ordered")
            continue
        if TABLE_PATTERN.match(line):
            sequence.append(f"table-{_count_table_columns(line)}")
    return sequence


def _is_external(destination: str) -> bool:
    lowered = destination.casefold()
    return lowered.startswith(("http://", "https://", "//", "mailto:", "data:"))


def _clean_url(value: str) -> str:
    return value.rstrip(").,;:")


def _extract_bare_urls(text: str) -> list[str]:
    without_markdown_links = INLINE_LINK_PATTERN.sub(" ", text)
    without_reference_definitions = REFERENCE_DEFINITION_PATTERN.sub(
        " ", without_markdown_links
    )
    return [
        _clean_url(match.group(0))
        for match in URL_PATTERN.finditer(without_reference_definitions)
    ]


def _visible_text_for_untranslated_check(line: str) -> str:
    line = INLINE_LINK_PATTERN.sub(lambda match: match.group("label"), line)
    line = REFERENCE_USAGE_PATTERN.sub(lambda match: match.group("label"), line)
    line = INLINE_CODE_PATTERN.sub(" ", line)
    line = URL_PATTERN.sub(" ", line)
    line = re.sub(r"^[ \t]{0,3}(?:#{1,6}|>|[-+*]|\d+[.)])[ \t]+", "", line)
    line = re.sub(r"[*_~|<>{}\[\]()]|\\.", " ", line)
    return line


def _untranslated_segments(masked: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for line_number, line in enumerate(masked.splitlines(), start=1):
        if not line.strip() or line.startswith(("    ", "\t")):
            continue
        if REFERENCE_DEFINITION_PATTERN.match(line):
            continue
        visible = _visible_text_for_untranslated_check(line)
        for segment in CJK_PATTERN.split(visible):
            words = ENGLISH_WORD_PATTERN.findall(segment)
            if len(words) < 12:
                continue
            proper_names = sum(word[0].isupper() for word in words)
            if proper_names / len(words) >= 0.65:
                continue
            excerpt = " ".join(words[:16])
            findings.append(
                {
                    "line": line_number,
                    "english_words": len(words),
                    "excerpt": excerpt,
                }
            )
    return findings


def _document_facts(text: str) -> DocumentFacts:
    masked, code_blocks, parse_errors = _mask_fenced_code(text)
    headings, anchors = _heading_facts(masked)
    inline_code = [
        match.group(2)
        for line in masked.splitlines()
        for match in INLINE_CODE_PATTERN.finditer(line)
    ]
    list_types: list[str] = []
    table_columns: list[int] = []
    blockquote_lines = 0
    for line in masked.splitlines():
        list_match = LIST_PATTERN.match(line)
        if list_match:
            list_types.append("bullet" if list_match.group("bullet") else "ordered")
        if TABLE_PATTERN.match(line):
            table_columns.append(_count_table_columns(line))
        if BLOCKQUOTE_PATTERN.match(line):
            blockquote_lines += 1
    stripped = text.strip()
    block_count = len(re.split(r"\n[ \t]*\n", stripped)) if stripped else 0
    links = _extract_links(masked)
    bare_urls = _extract_bare_urls(masked)
    return DocumentFacts(
        masked=masked,
        code_blocks=code_blocks,
        headings=headings,
        heading_anchors=anchors,
        inline_code=inline_code,
        block_count=block_count,
        list_types=list_types,
        table_columns=table_columns,
        blockquote_lines=blockquote_lines,
        structural_sequence=_structural_sequence(masked),
        links=links,
        bare_urls=bare_urls,
        untranslated_segments=_untranslated_segments(masked),
        parse_errors=parse_errors,
    )


def _difference_message(name: str, source: object, target: object) -> str:
    if isinstance(source, list) and isinstance(target, list):
        common = min(len(source), len(target))
        first_difference = next(
            (index for index in range(common) if source[index] != target[index]),
            common,
        )
        source_item = source[first_difference] if first_difference < len(source) else None
        target_item = target[first_difference] if first_difference < len(target) else None
        return (
            f"{name} differs at index {first_difference}: "
            f"source={_preview(source_item)}, target={_preview(target_item)}; "
            f"counts source={len(source)}, target={len(target)}"
        )
    return f"{name} differs: source={source!r}, target={target!r}"


def _preview(value: object, limit: int = 220) -> str:
    rendered = repr(value)
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3] + "..."


def _validate_image_files(target: Path, links: Iterable[Link]) -> list[str]:
    errors: list[str] = []
    for link in links:
        if not link.is_image or _is_external(link.destination):
            continue
        destination_path = urlsplit(link.destination).path
        resolved = (target.parent / unquote(destination_path)).resolve()
        if not resolved.is_file():
            errors.append(
                f"Referenced local image does not exist: {link.destination}"
            )
    return errors


def validate(source: Path, target: Path) -> tuple[dict[str, object], list[str]]:
    errors: list[str] = []
    source = source.resolve()
    target = target.resolve()
    if not source.is_file():
        return {}, [f"Source Markdown does not exist: {source}"]
    if not target.is_file():
        return {}, [f"Target Markdown does not exist: {target}"]
    if source == target:
        return {}, ["Source and target must be different files"]
    if source.suffix.casefold() != ".md" or target.suffix.casefold() != ".md":
        return {}, ["Source and target must both use the .md extension"]

    source_text = source.read_text(encoding="utf-8")
    target_text = target.read_text(encoding="utf-8")
    source_facts = _document_facts(source_text)
    target_facts = _document_facts(target_text)
    errors.extend(f"Source: {error}" for error in source_facts.parse_errors)
    errors.extend(f"Target: {error}" for error in target_facts.parse_errors)

    source_levels = [level for level, _ in source_facts.headings]
    target_levels = [level for level, _ in target_facts.headings]
    comparisons = (
        ("Heading level sequence", source_levels, target_levels),
        ("Separated content block count", source_facts.block_count, target_facts.block_count),
        ("List item type sequence", source_facts.list_types, target_facts.list_types),
        ("Table column sequence", source_facts.table_columns, target_facts.table_columns),
        ("Block quote line count", source_facts.blockquote_lines, target_facts.blockquote_lines),
        (
            "Structural element sequence",
            source_facts.structural_sequence,
            target_facts.structural_sequence,
        ),
        ("Fenced code blocks", source_facts.code_blocks, target_facts.code_blocks),
        ("Inline code sequence", source_facts.inline_code, target_facts.inline_code),
        ("External URL sequence", source_facts.bare_urls, target_facts.bare_urls),
    )
    for name, source_value, target_value in comparisons:
        if source_value != target_value:
            errors.append(_difference_message(name, source_value, target_value))

    source_images = [
        link.destination for link in source_facts.links if link.is_image
    ]
    target_images = [
        link.destination for link in target_facts.links if link.is_image
    ]
    if source_images != target_images:
        errors.append(
            _difference_message("Image destination sequence", source_images, target_images)
        )
    errors.extend(_validate_image_files(target, target_facts.links))

    source_regular = [link for link in source_facts.links if not link.is_image]
    target_regular = [link for link in target_facts.links if not link.is_image]
    source_kinds = [
        "fragment" if link.destination.startswith("#") else "fixed"
        for link in source_regular
    ]
    target_kinds = [
        "fragment" if link.destination.startswith("#") else "fixed"
        for link in target_regular
    ]
    if source_kinds != target_kinds:
        errors.append(
            _difference_message("Link kind sequence", source_kinds, target_kinds)
        )
    source_fixed = [
        link.destination
        for link in source_regular
        if not link.destination.startswith("#")
    ]
    target_fixed = [
        link.destination
        for link in target_regular
        if not link.destination.startswith("#")
    ]
    if source_fixed != target_fixed:
        errors.append(
            _difference_message(
                "External and relative link destination sequence",
                source_fixed,
                target_fixed,
            )
        )
    for link in target_regular:
        if not link.destination.startswith("#"):
            continue
        fragment = unquote(link.destination[1:]).casefold()
        if fragment not in target_facts.heading_anchors:
            errors.append(
                f"Internal fragment does not resolve to a target heading: "
                f"{link.destination}"
            )

    if source_text == target_text:
        errors.append("Target is identical to source")
    cjk_count = len(CJK_PATTERN.findall(target_facts.masked))
    if cjk_count == 0:
        errors.append("Target contains no Simplified Chinese text")
    if target_facts.untranslated_segments:
        errors.append(
            "Possible untranslated English prose outside code: "
            + json.dumps(target_facts.untranslated_segments, ensure_ascii=False)
        )

    metrics: dict[str, object] = {
        "source": str(source),
        "target": str(target),
        "headings": len(source_facts.headings),
        "content_blocks": source_facts.block_count,
        "list_items": len(source_facts.list_types),
        "table_rows": len(source_facts.table_columns),
        "blockquote_lines": source_facts.blockquote_lines,
        "code_blocks": len(source_facts.code_blocks),
        "inline_code_spans": len(source_facts.inline_code),
        "images": len(source_images),
        "links": len(source_regular),
        "target_cjk_characters": cjk_count,
    }
    return metrics, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="English source Markdown file")
    parser.add_argument("target", type=Path, help="Simplified Chinese Markdown file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        metrics, errors = validate(args.source, args.target)
    except (OSError, UnicodeError) as exc:
        errors = [str(exc)]
        metrics = {}
    if errors:
        print(
            json.dumps(
                {"status": "error", **metrics, "errors": errors},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "ok", **metrics}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
