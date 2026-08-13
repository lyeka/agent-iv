#!/usr/bin/env python3
"""Validate a webpage-to-local-markdown output bundle."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ALLOWED_IMAGE_EXTENSIONS = {
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".webp",
}
IMAGE_PATTERN = re.compile(r"!\[[^\]]*]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
RAW_HTML_PATTERN = re.compile(
    r"<\s*/?\s*[A-Za-z][A-Za-z0-9-]*(?:\s+[^>]*)?\s*/?>",
    re.IGNORECASE,
)
SOURCE_PATTERN = re.compile(
    r"^\*\*Source:\*\*\s+\[[^\]]+]\((https?://[^)]+)\)\s*$",
    re.MULTILINE,
)


class ValidationError(RuntimeError):
    """Raised when an output bundle violates the contract."""


def _run_pandoc_json(path: Path, input_format: str) -> dict[str, Any]:
    process = subprocess.run(
        ["pandoc", str(path), "-f", input_format, "-t", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise ValidationError(
            f"Pandoc could not parse {path.name}: {process.stderr.strip()}"
        )
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Pandoc returned invalid JSON for {path.name}") from exc


def _collect_tokens(document: dict[str, Any]) -> Counter[str]:
    tokens: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("t") == "Str" and isinstance(value.get("c"), str):
            tokens.extend(re.findall(r"\w+", value["c"].casefold(), re.UNICODE))
        for nested in value.values():
            walk(nested)

    walk(document.get("blocks", []))
    return Counter(tokens)


def _validate_image_signature(path: Path) -> None:
    data = path.read_bytes()[:64]
    suffix = path.suffix.casefold()
    valid = False
    if suffix in {".jpg", ".jpeg"}:
        valid = data.startswith(b"\xff\xd8\xff")
    elif suffix == ".png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix == ".gif":
        valid = data.startswith((b"GIF87a", b"GIF89a"))
    elif suffix == ".webp":
        valid = data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    elif suffix == ".avif":
        valid = b"ftypavif" in data or b"ftypavis" in data
    elif suffix == ".svg":
        sample = path.read_text(encoding="utf-8", errors="ignore")[:2048].casefold()
        valid = "<svg" in sample and "<script" not in sample
    if not valid:
        raise ValidationError(f"Image has an invalid or unsafe signature: {path}")


def validate_bundle(
    bundle_dir: Path,
    *,
    source_html: Path | None = None,
) -> dict[str, Any]:
    """Validate a bundle and optionally compare it with cleaned source HTML."""

    bundle_dir = bundle_dir.resolve()
    if not bundle_dir.is_dir():
        raise ValidationError(f"Bundle directory does not exist: {bundle_dir}")

    root_entries = sorted(bundle_dir.iterdir())
    markdown_files = [path for path in root_entries if path.suffix.casefold() == ".md"]
    unexpected_root = [
        path
        for path in root_entries
        if path not in markdown_files and not (path.is_dir() and path.name == "images")
    ]
    if len(markdown_files) != 1:
        raise ValidationError(
            f"Expected exactly one Markdown file, found {len(markdown_files)}"
        )
    if unexpected_root:
        names = ", ".join(path.name for path in unexpected_root)
        raise ValidationError(f"Unexpected bundle resources: {names}")

    markdown_path = markdown_files[0]
    markdown = markdown_path.read_text(encoding="utf-8")
    if len(markdown.strip()) < 200:
        raise ValidationError("Markdown body is unexpectedly short")
    first_nonempty = next((line for line in markdown.splitlines() if line.strip()), "")
    if not re.match(r"^#\s+\S", first_nonempty):
        raise ValidationError("Markdown must begin with an H1 title")
    source_match = SOURCE_PATTERN.search(markdown)
    if not source_match:
        raise ValidationError("Markdown is missing a public Source metadata link")
    if RAW_HTML_PATTERN.search(markdown):
        raise ValidationError("Markdown contains raw HTML tags")

    image_dir = bundle_dir / "images"
    image_paths: list[Path] = []
    if image_dir.exists():
        if not image_dir.is_dir():
            raise ValidationError("images must be a directory")
        for path in sorted(image_dir.rglob("*")):
            if path.is_dir():
                continue
            if path.parent != image_dir:
                raise ValidationError("Nested image directories are not allowed")
            if path.suffix.casefold() not in ALLOWED_IMAGE_EXTENSIONS:
                raise ValidationError(f"Unsupported image resource: {path.name}")
            _validate_image_signature(path)
            image_paths.append(path)

    references = IMAGE_PATTERN.findall(markdown)
    resolved_references: list[Path] = []
    for reference in references:
        if re.match(r"^(?:https?:|data:|//)", reference, re.IGNORECASE):
            raise ValidationError(f"Image reference is not local: {reference}")
        resolved = (markdown_path.parent / reference).resolve()
        try:
            resolved.relative_to(bundle_dir)
        except ValueError as exc:
            raise ValidationError(
                f"Image reference escapes the bundle: {reference}"
            ) from exc
        if not resolved.is_file():
            raise ValidationError(f"Referenced image does not exist: {reference}")
        resolved_references.append(resolved)

    unreferenced = sorted(set(image_paths) - set(resolved_references))
    if unreferenced:
        names = ", ".join(path.name for path in unreferenced)
        raise ValidationError(f"Unreferenced image resources: {names}")

    source_tokens = None
    markdown_tokens = None
    if source_html is not None:
        source_html = source_html.resolve()
        if not source_html.is_file():
            raise ValidationError(f"Cleaned source HTML does not exist: {source_html}")
        source_document = _run_pandoc_json(source_html, "html")
        markdown_document = _run_pandoc_json(markdown_path, "gfm")
        source_counter = _collect_tokens(source_document)
        markdown_counter = _collect_tokens(markdown_document)
        source_tokens = sum(source_counter.values())
        markdown_tokens = sum(markdown_counter.values())
        if source_counter != markdown_counter:
            missing = list((source_counter - markdown_counter).items())[:20]
            extra = list((markdown_counter - source_counter).items())[:20]
            raise ValidationError(
                "Markdown text differs from cleaned source; "
                f"missing={missing}, extra={extra}"
            )

    return {
        "status": "ok",
        "bundle": str(bundle_dir),
        "markdown": str(markdown_path),
        "images": len(image_paths),
        "image_references": len(references),
        "source_url": source_match.group(1),
        "source_tokens": source_tokens,
        "markdown_tokens": markdown_tokens,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", type=Path)
    parser.add_argument(
        "--source-html",
        type=Path,
        help="Optional cleaned HTML used for AST text-completeness comparison",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = validate_bundle(args.bundle_dir, source_html=args.source_html)
    except (OSError, ValidationError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
