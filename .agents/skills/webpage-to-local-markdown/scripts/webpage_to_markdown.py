#!/usr/bin/env python3
"""Convert one public webpage into a validated local Markdown bundle."""

from __future__ import annotations

import argparse
import base64
from copy import copy
from datetime import datetime, timezone
import hashlib
import html as html_stdlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit
import uuid

from bs4 import BeautifulSoup, Comment, Tag
import requests

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from validate_markdown_bundle import ValidationError, validate_bundle  # noqa: E402


RENDER_REQUIRED_EXIT = 20
MIN_ARTICLE_CHARACTERS = 400
MAX_HTML_BYTES = 25 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
REQUEST_TIMEOUT = 30

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 webpage-to-local-markdown/1.0"
)

ARTICLE_SELECTORS = (
    "article",
    '[itemprop="articleBody"]',
    "main article",
    ".article-body",
    ".article-content",
    ".post-content",
    ".entry-content",
    ".story-body",
    ".content-body",
    ".markdown-body",
    "main",
    '[role="main"]',
)

BOILERPLATE_TOKENS = {
    "ad",
    "ads",
    "advert",
    "advertisement",
    "banner",
    "breadcrumb",
    "breadcrumbs",
    "comments",
    "cookie",
    "cookies",
    "copyright",
    "cta",
    "footer",
    "menu",
    "modal",
    "newsletter",
    "pagination",
    "promo",
    "promotion",
    "recommendation",
    "recommendations",
    "related",
    "share",
    "sharing",
    "sidebar",
    "social",
    "subscribe",
    "subscription",
    "toolbar",
    "tracking",
}

PLACEHOLDER_RE = re.compile(
    r"(enable javascript|javascript is required|please turn on javascript|"
    r"loading(?:\s+content)?[.…]*$|checking your browser)",
    re.IGNORECASE,
)
ACCESS_CONTROL_RE = re.compile(
    r"\b(sign in|log in|subscribe to continue|paywall|captcha)\b",
    re.IGNORECASE,
)
DECORATIVE_IMAGE_RE = re.compile(
    r"(?:^|[-_/])(avatar|badge|button|favicon|icon|pixel|spacer|spinner|"
    r"tracking|transparent)(?:[-_.?/]|$)",
    re.IGNORECASE,
)
HERO_TOKEN_RE = re.compile(
    r"(?:^|[-_\s])(cover|featured|hero|lead|masthead)(?:[-_\s]|$)",
    re.IGNORECASE,
)
SOURCE_METADATA_RE = re.compile(
    r"^\*\*Source:\*\*\s+\[[^\]]+]\((https?://[^)]+)\)\s*$",
    re.MULTILINE,
)

IMAGE_CONTENT_TYPES = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


class ConversionError(RuntimeError):
    """Raised for a conversion failure that must not publish partial output."""


class RenderRequired(ConversionError):
    """Raised when the public page needs browser rendering."""


def emit(payload: dict, *, stream=sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def validate_public_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ConversionError("Only public http/https URLs are supported")
    if parsed.username or parsed.password:
        raise ConversionError("URLs containing embedded credentials are not supported")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    port = parsed.port
    if port and not (
        (parsed.scheme.casefold() == "http" and port == 80)
        or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(
        (parsed.scheme.casefold(), hostname, path, parsed.query, "")
    )


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html_stdlib.unescape(value or "")).strip()


def slugify(value: str, *, fallback: str, limit: int = 80) -> str:
    value = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    pieces: list[str] = []
    last_was_separator = False
    for character in value:
        category = unicodedata.category(character)
        if category[0] in {"L", "N"}:
            pieces.append(character)
            last_was_separator = False
        elif not last_was_separator:
            pieces.append("-")
            last_was_separator = True
    slug = "".join(pieces).strip("-")
    if not slug:
        slug = fallback
    return slug[:limit].strip("-") or fallback


def attribute_tokens(tag: Tag) -> set[str]:
    values: list[str] = []
    for name in ("id", "class", "role", "aria-label", "data-testid"):
        value = tag.get(name)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    tokens: set[str] = set()
    for value in values:
        tokens.update(
            token
            for token in re.split(r"[^a-z0-9]+", value.casefold())
            if token
        )
    return tokens


def is_boilerplate(tag: Tag) -> bool:
    tokens = attribute_tokens(tag)
    return bool(tokens & BOILERPLATE_TOKENS)


def fetch_html(session: requests.Session, url: str) -> tuple[str, str]:
    response = session.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
        stream=True,
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.casefold() and "xhtml" not in content_type.casefold():
        raise ConversionError(f"URL did not return HTML: {content_type or 'unknown'}")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_HTML_BYTES:
            raise ConversionError("HTML response exceeds the 25 MiB safety limit")
        chunks.append(chunk)
    content = b"".join(chunks)
    encoding = response.encoding or response.apparent_encoding or "utf-8"
    return content.decode(encoding, errors="replace"), response.url


def load_rendered_html(path: Path) -> str:
    if not path.is_file():
        raise ConversionError(f"Rendered HTML file does not exist: {path}")
    if path.stat().st_size > MAX_HTML_BYTES:
        raise ConversionError("Rendered HTML exceeds the 25 MiB safety limit")
    return path.read_text(encoding="utf-8", errors="replace")


def tag_text(tag: Tag) -> str:
    return clean_text(tag.get_text(" ", strip=True))


def content_score(tag: Tag, semantic_bonus: int = 0) -> float:
    text = tag_text(tag)
    if not text:
        return float("-inf")
    text_length = len(text)
    paragraph_count = len(tag.find_all("p"))
    heading_count = len(tag.find_all(re.compile(r"^h[1-6]$")))
    link_text_length = sum(len(tag_text(anchor)) for anchor in tag.find_all("a"))
    link_density = link_text_length / max(text_length, 1)
    penalty = text_length * min(link_density, 0.9) * 1.4
    if is_boilerplate(tag):
        penalty += 5000
    return (
        text_length
        + paragraph_count * 120
        + heading_count * 80
        + semantic_bonus
        - penalty
    )


def find_content_root(soup: BeautifulSoup) -> Tag:
    candidates: dict[int, tuple[Tag, int]] = {}
    for index, selector in enumerate(ARTICLE_SELECTORS):
        bonus = max(600, 4000 - index * 250)
        for tag in soup.select(selector):
            if isinstance(tag, Tag):
                current = candidates.get(id(tag))
                if current is None or bonus > current[1]:
                    candidates[id(tag)] = (tag, bonus)

    body = soup.body or soup
    for tag in body.find_all(["article", "main", "section", "div"]):
        if not isinstance(tag, Tag):
            continue
        if len(tag_text(tag)) < 200:
            continue
        candidates.setdefault(id(tag), (tag, 0))
    if isinstance(body, Tag):
        candidates.setdefault(id(body), (body, -2000))

    if not candidates:
        raise ConversionError("No readable content container was found")
    return max(
        candidates.values(),
        key=lambda item: content_score(item[0], item[1]),
    )[0]


def iter_json_objects(value):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_json_objects(nested)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_objects(item)


def json_ld_metadata(soup: BeautifulSoup) -> dict[str, str]:
    article_types = {
        "article",
        "blogposting",
        "newsarticle",
        "report",
        "scholarlyarticle",
        "techarticle",
    }
    best: dict | None = None
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (TypeError, json.JSONDecodeError):
            continue
        for candidate in iter_json_objects(payload):
            raw_type = candidate.get("@type", "")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if any(str(item).casefold() in article_types for item in types):
                best = candidate
                break
        if best:
            break
    if not best:
        return {}

    author = best.get("author")
    if isinstance(author, list):
        names = [
            clean_text(item.get("name") if isinstance(item, dict) else str(item))
            for item in author
        ]
        author_text = ", ".join(name for name in names if name)
    elif isinstance(author, dict):
        author_text = clean_text(author.get("name"))
    else:
        author_text = clean_text(str(author or ""))
    return {
        "title": clean_text(best.get("headline") or best.get("name")),
        "author": author_text,
        "published": clean_text(best.get("datePublished")),
        "description": clean_text(best.get("description")),
        "image": clean_text(
            best.get("image", {}).get("url")
            if isinstance(best.get("image"), dict)
            else (
                best.get("image", [""])[0]
                if isinstance(best.get("image"), list)
                else best.get("image")
            )
        ),
    }


def meta_content(soup: BeautifulSoup, *names: str) -> str:
    expected = {name.casefold() for name in names}
    for tag in soup.find_all("meta"):
        declared = clean_text(
            tag.get("name") or tag.get("property") or tag.get("itemprop")
        ).casefold()
        if declared in expected and tag.get("content"):
            return clean_text(tag["content"])
    return ""


def extract_metadata(
    soup: BeautifulSoup,
    root: Tag,
    source_url: str,
) -> dict[str, str]:
    structured = json_ld_metadata(soup)
    h1 = root.find("h1") or soup.find("h1")
    html_title = clean_text(soup.title.string if soup.title and soup.title.string else "")
    title = (
        structured.get("title")
        or clean_text(h1.get_text(" ", strip=True) if h1 else "")
        or meta_content(soup, "og:title", "twitter:title")
        or html_title
    )
    if not title:
        parsed = urlsplit(source_url)
        title = clean_text(Path(parsed.path).name.replace("-", " ")) or parsed.hostname or "page"

    author = (
        structured.get("author")
        or meta_content(soup, "author", "article:author", "byl")
    )
    if not author:
        author_tag = soup.find(attrs={"rel": re.compile(r"\bauthor\b", re.I)})
        if author_tag:
            author = tag_text(author_tag)
    published = (
        structured.get("published")
        or meta_content(
            soup,
            "article:published_time",
            "datePublished",
            "date",
            "pubdate",
        )
    )
    if not published:
        time_tag = root.find("time", attrs={"datetime": True})
        if time_tag:
            published = clean_text(time_tag.get("datetime"))
    description = (
        structured.get("description")
        or meta_content(soup, "description", "og:description", "twitter:description")
    )
    canonical_tag = soup.find("link", attrs={"rel": re.compile(r"\bcanonical\b", re.I)})
    canonical = urljoin(
        source_url,
        clean_text(canonical_tag.get("href") if canonical_tag else "") or source_url,
    )
    canonical = validate_public_url(canonical)
    return {
        "title": title,
        "author": author,
        "published": published,
        "description": description,
        "canonical": canonical,
        "structured_image": structured.get("image", ""),
        "og_image": meta_content(soup, "og:image", "twitter:image"),
    }


def parse_dimension(value) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def parse_srcset(value: str) -> str:
    candidates: list[tuple[float, str]] = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.rsplit(None, 1)
        url = parts[0]
        weight = 1.0
        if len(parts) == 2:
            descriptor = parts[1].casefold()
            try:
                if descriptor.endswith("w"):
                    weight = float(descriptor[:-1])
                elif descriptor.endswith("x"):
                    weight = float(descriptor[:-1]) * 10000
            except ValueError:
                weight = 1.0
        candidates.append((weight, url))
    return max(candidates, default=(0, ""))[1]


def image_candidate(img: Tag) -> str:
    for attribute in (
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-url",
        "data-webpage-md-background",
    ):
        value = clean_text(img.get(attribute))
        if value and not value.startswith("data:image/gif;base64,R0lGOD"):
            return value
    srcsets: list[str] = []
    if img.get("srcset"):
        srcsets.append(str(img["srcset"]))
    picture = img.find_parent("picture")
    if picture:
        srcsets.extend(
            str(source["srcset"])
            for source in picture.find_all("source", attrs={"srcset": True})
        )
    best = ""
    best_weight = -1
    for srcset in srcsets:
        candidate = parse_srcset(srcset)
        if not candidate:
            continue
        match = re.search(r"(?:\s|^)(\d+(?:\.\d+)?)([wx])(?:\s|$)", srcset)
        weight = float(match.group(1)) if match else 1
        if match and match.group(2).casefold() == "x":
            weight *= 10000
        if weight > best_weight:
            best = candidate
            best_weight = weight
    if best:
        return best
    return clean_text(img.get("src"))


def unwrap_image_proxy(url: str, base_url: str) -> str:
    absolute = urljoin(base_url, html_stdlib.unescape(url))
    parsed = urlsplit(absolute)
    if parsed.path.rstrip("/").endswith("/_next/image"):
        target = parse_qs(parsed.query).get("url", [""])[0]
        if target:
            return urljoin(base_url, unquote(target))
    return absolute


def tag_is_descendant(tag: Tag, ancestor: Tag) -> bool:
    parent = tag.parent
    while isinstance(parent, Tag):
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def find_external_hero(
    soup: BeautifulSoup,
    root: Tag,
    metadata: dict[str, str],
    base_url: str,
) -> Tag | None:
    root_sources = {
        normalize_url(unwrap_image_proxy(source, base_url))
        for img in root.find_all("img")
        if (source := image_candidate(img))
        and not source.startswith("data:")
    }
    expected = {
        normalize_url(unwrap_image_proxy(url, base_url))
        for url in (metadata.get("structured_image"), metadata.get("og_image"))
        if url and not url.startswith("data:")
    }
    for img in soup.find_all("img"):
        if tag_is_descendant(img, root):
            continue
        source = image_candidate(img)
        if not source:
            continue
        absolute = normalize_url(unwrap_image_proxy(source, base_url))
        if absolute in root_sources:
            continue
        tokens = " ".join(
            [
                str(img.get("class", "")),
                str(img.get("id", "")),
                str(img.parent.get("class", "")) if isinstance(img.parent, Tag) else "",
            ]
        )
        if absolute in expected or HERO_TOKEN_RE.search(tokens):
            return img
    return None


def clone_content(root: Tag, external_hero: Tag | None) -> tuple[BeautifulSoup, Tag]:
    fragment = BeautifulSoup(f"<article>{root.decode_contents()}</article>", "lxml")
    content = fragment.find("article")
    if content is None:
        raise ConversionError("Failed to clone the selected content")
    if external_hero is not None:
        hero_fragment = BeautifulSoup(str(copy(external_hero)), "lxml")
        hero = hero_fragment.find("img")
        if hero:
            hero["data-output-role"] = "hero"
            content.insert(0, hero)
    return fragment, content


def make_tag(context: Tag, name: str, **attributes) -> Tag:
    root: Tag | BeautifulSoup = context
    while root.parent is not None:
        root = root.parent
    if not isinstance(root, BeautifulSoup):
        raise ConversionError("Content fragment is detached from its parser")
    return root.new_tag(name, **attributes)


def remove_boilerplate(content: Tag) -> None:
    for comment in content.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for tag in list(
        content.find_all(
            [
                "script",
                "style",
                "noscript",
                "template",
                "iframe",
                "form",
                "button",
                "nav",
                "canvas",
            ]
        )
    ):
        tag.decompose()
    for tag in list(content.find_all(True)):
        if not tag.parent:
            continue
        if tag.get("hidden") is not None or tag.get("aria-hidden") == "true":
            tag.decompose()
            continue
        if is_boilerplate(tag):
            tag.decompose()


def meaningful_svg(svg: Tag) -> bool:
    if svg.find_parent("figure"):
        return True
    width = parse_dimension(svg.get("width"))
    height = parse_dimension(svg.get("height"))
    if width and height and width >= 120 and height >= 80:
        return True
    label = clean_text(
        svg.get("aria-label")
        or (svg.find("title").get_text(" ", strip=True) if svg.find("title") else "")
        or (svg.find("desc").get_text(" ", strip=True) if svg.find("desc") else "")
    )
    token_text = " ".join(attribute_tokens(svg))
    return bool(
        (label and len(label) > 3)
        or re.search(r"(chart|diagram|graph|illustration|map|plot)", token_text)
    )


def sanitize_svg_bytes(data: bytes) -> bytes:
    soup = BeautifulSoup(data.decode("utf-8", errors="replace"), "xml")
    svg = soup.find("svg")
    if svg is None:
        raise ConversionError("Downloaded SVG does not contain an svg element")
    for unsafe in list(svg.find_all(["script", "foreignObject", "iframe", "object", "embed"])):
        unsafe.decompose()
    for tag in svg.find_all(True):
        for attribute, value in list(tag.attrs.items()):
            lowered = attribute.casefold()
            rendered = " ".join(value) if isinstance(value, list) else str(value)
            if lowered.startswith("on") or rendered.strip().casefold().startswith("javascript:"):
                del tag.attrs[attribute]
    if not svg.get("xmlns"):
        svg["xmlns"] = "http://www.w3.org/2000/svg"
    return str(svg).encode("utf-8")


def convert_inline_svgs(content: Tag) -> None:
    for svg in list(content.find_all("svg")):
        if not meaningful_svg(svg):
            svg.decompose()
            continue
        label = clean_text(
            svg.get("aria-label")
            or (svg.find("title").get_text(" ", strip=True) if svg.find("title") else "")
            or (svg.find("desc").get_text(" ", strip=True) if svg.find("desc") else "")
        )
        payload = base64.b64encode(sanitize_svg_bytes(str(svg).encode("utf-8"))).decode()
        replacement = make_tag(content, "img")
        replacement["src"] = f"data:image/svg+xml;base64,{payload}"
        replacement["alt"] = label
        svg.replace_with(replacement)


def add_annotated_background_images(content: Tag) -> None:
    for tag in list(content.find_all(attrs={"data-webpage-md-background": True})):
        source = clean_text(tag.get("data-webpage-md-background"))
        del tag.attrs["data-webpage-md-background"]
        if not source:
            continue
        existing = tag.find("img", recursive=False)
        if existing and image_candidate(existing) == source:
            continue
        img = make_tag(content, "img")
        img["src"] = source
        img["alt"] = clean_text(tag.get("aria-label"))
        if HERO_TOKEN_RE.search(" ".join(attribute_tokens(tag))):
            img["data-output-role"] = "hero"
        tag.insert(0, img)


def prepare_pictures(content: Tag) -> None:
    for picture in list(content.find_all("picture")):
        img = picture.find("img")
        if img is None:
            picture.decompose()
            continue
        source = image_candidate(img)
        if source:
            img["src"] = source
        img.extract()
        picture.replace_with(img)


def is_decorative_image(img: Tag, source: str) -> bool:
    width = parse_dimension(img.get("width"))
    height = parse_dimension(img.get("height"))
    alt = clean_text(img.get("alt"))
    if width and height and width <= 32 and height <= 32 and not alt:
        return True
    if DECORATIVE_IMAGE_RE.search(source) and not img.find_parent("figure") and not alt:
        return True
    return False


def decode_data_uri(uri: str) -> tuple[bytes, str]:
    match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", uri, re.I | re.S)
    if not match:
        raise ConversionError("Malformed image data URI")
    media_type = (match.group(1) or "application/octet-stream").casefold()
    payload = match.group(3)
    if match.group(2):
        try:
            data = base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise ConversionError("Malformed base64 image data URI") from exc
    else:
        data = unquote(payload).encode("utf-8")
    return data, media_type


def detect_extension(data: bytes, content_type: str, source: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type in IMAGE_CONTENT_TYPES:
        return IMAGE_CONTENT_TYPES[media_type]
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if b"ftypavif" in data[:64] or b"ftypavis" in data[:64]:
        return ".avif"
    if b"<svg" in data[:2048].casefold():
        return ".svg"
    suffix = Path(urlsplit(source).path).suffix.casefold()
    if suffix in {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    raise ConversionError(f"Unsupported or unrecognized content image: {source}")


def download_image(
    session: requests.Session,
    source: str,
    *,
    base_url: str,
    referer: str,
) -> tuple[bytes, str, str]:
    if source.startswith("data:"):
        data, content_type = decode_data_uri(source)
        resolved = f"data:{hashlib.sha256(data).hexdigest()}"
    else:
        resolved = unwrap_image_proxy(source, base_url)
        parsed = urlsplit(resolved)
        if parsed.scheme.casefold() not in {"http", "https"}:
            raise ConversionError(f"Unsupported image URL: {resolved}")
        response = session.get(
            resolved,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": referer,
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ConversionError(
                    f"Image exceeds the 50 MiB safety limit: {resolved}"
                )
            chunks.append(chunk)
        data = b"".join(chunks)
        content_type = response.headers.get("content-type", "")
        resolved = response.url
    extension = detect_extension(data, content_type, resolved)
    if extension == ".svg":
        data = sanitize_svg_bytes(data)
    return data, extension, resolved


def figure_caption(img: Tag) -> str:
    figure = img.find_parent("figure")
    if figure:
        caption = figure.find("figcaption")
        if caption:
            return tag_text(caption)
    return ""


def unique_basename(base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def process_images(
    content: Tag,
    *,
    session: requests.Session,
    base_url: str,
    images_dir: Path,
) -> int:
    used_names: set[str] = set()
    assets: dict[str, tuple[str, bytes]] = {}
    figure_number = 0
    image_count = 0

    for img in list(content.find_all("img")):
        source = image_candidate(img)
        if not source:
            if clean_text(img.get("alt")):
                raise RenderRequired("A content image has no resolved source")
            img.decompose()
            continue
        if is_decorative_image(img, source):
            img.decompose()
            continue

        role = clean_text(img.get("data-output-role")).casefold()
        token_text = " ".join(
            [
                str(img.get("class", "")),
                str(img.get("id", "")),
                str(img.parent.get("class", "")) if isinstance(img.parent, Tag) else "",
            ]
        )
        if not role and HERO_TOKEN_RE.search(token_text):
            role = "hero"
        caption = figure_caption(img)
        alt = clean_text(img.get("alt") or img.get("title"))
        if role == "hero":
            preferred_name = "hero"
        else:
            figure_number += 1
            preferred_name = slugify(
                caption or alt,
                fallback=f"figure-{figure_number:02d}",
                limit=64,
            )

        data, extension, resolved = download_image(
            session,
            source,
            base_url=base_url,
            referer=base_url,
        )
        digest = hashlib.sha256(data).hexdigest()
        asset_key = f"{digest}:{extension}"
        if asset_key in assets:
            filename = assets[asset_key][0]
        else:
            basename = unique_basename(preferred_name, used_names)
            filename = f"{basename}{extension}"
            images_dir.mkdir(parents=True, exist_ok=True)
            (images_dir / filename).write_bytes(data)
            assets[asset_key] = (filename, data)

        img.attrs = {
            "src": f"images/{filename}",
            "alt": alt or caption or ("Hero image" if role == "hero" else f"Figure {figure_number}"),
        }
        image_count += 1

    return image_count


def normalize_figures(content: Tag) -> None:
    for figure in list(content.find_all("figure")):
        img = figure.find("img")
        caption = figure.find("figcaption")
        if img is None:
            if caption:
                caption.unwrap()
            figure.unwrap()
            continue
        image_paragraph = make_tag(content, "p")
        image_paragraph.append(img.extract())
        if caption:
            caption_text = tag_text(caption)
            caption_paragraph = make_tag(content, "p")
            emphasis = make_tag(content, "em")
            emphasis.string = caption_text
            caption_paragraph.append(emphasis)
            figure.replace_with(image_paragraph)
            image_paragraph.insert_after(caption_paragraph)
        else:
            figure.replace_with(image_paragraph)


def simplify_tables(content: Tag) -> None:
    for table in content.find_all("table"):
        caption = table.find("caption")
        if caption:
            caption_paragraph = make_tag(content, "p")
            emphasis = make_tag(content, "em")
            emphasis.string = tag_text(caption)
            caption_paragraph.append(emphasis)
            table.insert_before(caption_paragraph)
            caption.decompose()
        for cell in table.find_all(["th", "td"]):
            values: list[str] = []
            list_items = cell.find_all("li")
            if list_items:
                values.extend(tag_text(item) for item in list_items if tag_text(item))
            else:
                values.extend(
                    clean_text(value)
                    for value in cell.stripped_strings
                    if clean_text(value)
                )
            cell.clear()
            cell.string = "; ".join(values)
        for removable in list(table.find_all(["colgroup", "col"])):
            removable.decompose()


def sanitize_links(content: Tag, base_url: str) -> None:
    for anchor in content.find_all("a"):
        href = clean_text(anchor.get("href"))
        if not href:
            anchor.unwrap()
            continue
        if href.startswith("#"):
            href = f"{base_url.split('#', 1)[0]}{href}"
        else:
            href = urljoin(base_url, href)
        parsed = urlsplit(href)
        if parsed.scheme.casefold() not in {"http", "https", "mailto", "tel"}:
            anchor.unwrap()
            continue
        anchor.attrs = {"href": href}


def strip_attributes_and_wrappers(content: Tag) -> None:
    for tag in content.find_all(True):
        if tag.name == "a":
            tag.attrs = {"href": tag.get("href", "")}
        elif tag.name == "img":
            tag.attrs = {
                "src": tag.get("src", ""),
                "alt": clean_text(tag.get("alt")),
            }
        elif tag.name == "code":
            language = ""
            for class_name in tag.get("class", []):
                if str(class_name).startswith("language-"):
                    language = str(class_name)
                    break
            tag.attrs = {"class": [language]} if language else {}
        elif tag.name == "ol" and tag.get("start"):
            tag.attrs = {"start": tag.get("start")}
        else:
            tag.attrs = {}
    for name in ("div", "section", "main", "article", "header", "footer", "span"):
        for tag in list(content.find_all(name)):
            tag.unwrap()


def remove_duplicate_title(content: Tag, title: str) -> None:
    h1 = content.find("h1")
    if h1 and clean_text(h1.get_text(" ", strip=True)).casefold() == title.casefold():
        h1.decompose()


def append_metadata_line(
    document: BeautifulSoup,
    body: Tag,
    label: str,
    value: str,
    *,
    link: str | None = None,
) -> None:
    if not value:
        return
    paragraph = document.new_tag("p")
    strong = document.new_tag("strong")
    strong.string = f"{label}:"
    paragraph.append(strong)
    paragraph.append(" ")
    if link:
        anchor = document.new_tag("a", href=link)
        anchor.string = value
        paragraph.append(anchor)
    else:
        paragraph.append(value)
    body.append(paragraph)


def build_clean_document(content: Tag, metadata: dict[str, str]) -> BeautifulSoup:
    document = BeautifulSoup(
        "<!doctype html><html><head><meta charset='utf-8'></head><body></body></html>",
        "lxml",
    )
    body = document.body
    assert body is not None
    title = document.new_tag("h1")
    title.string = metadata["title"]
    body.append(title)
    append_metadata_line(
        document,
        body,
        "Source",
        metadata["canonical"],
        link=metadata["canonical"],
    )
    append_metadata_line(document, body, "Author", metadata.get("author", ""))
    append_metadata_line(document, body, "Published", metadata.get("published", ""))
    append_metadata_line(
        document,
        body,
        "Saved locally",
        datetime.now(timezone.utc).date().isoformat(),
    )
    if metadata.get("description"):
        blockquote = document.new_tag("blockquote")
        paragraph = document.new_tag("p")
        paragraph.string = metadata["description"]
        blockquote.append(paragraph)
        body.append(blockquote)
    for child in list(content.contents):
        body.append(child.extract() if hasattr(child, "extract") else child)
    return document


def convert_raw_tables(markdown: str) -> str:
    pattern = re.compile(r"<table\b[\s\S]*?</table>", re.IGNORECASE)

    def replace(match: re.Match) -> str:
        soup = BeautifulSoup(match.group(), "lxml")
        table = soup.find("table")
        if table is None:
            return clean_text(soup.get_text(" ", strip=True))
        rows = table.find_all("tr")
        if not rows:
            return clean_text(table.get_text(" ", strip=True))
        header_cells = rows[0].find_all(["th", "td"])
        if not header_cells:
            return clean_text(table.get_text(" ", strip=True))

        def cell_text(cell: Tag) -> str:
            value = clean_text(cell.get_text("; ", strip=True)).replace("|", r"\|")
            return value

        headers = [cell_text(cell) for cell in header_cells]
        output = [
            f"| {' | '.join(headers)} |",
            f"| {' | '.join('---' for _ in headers)} |",
        ]
        for row in rows[1:]:
            cells = [cell_text(cell) for cell in row.find_all(["th", "td"])]
            if not cells:
                continue
            cells.extend("" for _ in range(len(headers) - len(cells)))
            output.append(f"| {' | '.join(cells[:len(headers)])} |")
        return "\n".join(output)

    return pattern.sub(replace, markdown)


def run_pandoc(clean_html: Path, markdown_path: Path) -> None:
    process = subprocess.run(
        [
            "pandoc",
            str(clean_html),
            "-f",
            "html",
            "-t",
            "gfm",
            "--wrap=none",
            "--markdown-headings=atx",
            "-o",
            str(markdown_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise ConversionError(f"Pandoc conversion failed: {process.stderr.strip()}")
    markdown = markdown_path.read_text(encoding="utf-8")
    markdown = convert_raw_tables(markdown)
    markdown_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")


def existing_source(directory: Path) -> str:
    markdown_files = sorted(directory.glob("*.md"))
    if len(markdown_files) != 1:
        return ""
    try:
        markdown = markdown_files[0].read_text(encoding="utf-8")
    except OSError:
        return ""
    match = SOURCE_METADATA_RE.search(markdown)
    return normalize_url(match.group(1)) if match else ""


def domain_slug(url: str) -> str:
    hostname = (urlsplit(url).hostname or "source").removeprefix("www.")
    return slugify(hostname.replace(".", " "), fallback="source", limit=40)


def choose_target(
    output_root: Path,
    *,
    title_slug: str,
    canonical: str,
) -> tuple[Path, bool]:
    normalized_source = normalize_url(canonical)
    for candidate in sorted(output_root.iterdir()):
        if candidate.is_dir() and existing_source(candidate) == normalized_source:
            return candidate, True

    target = output_root / title_slug
    if not target.exists():
        return target, False
    target = output_root / f"{title_slug}-{domain_slug(canonical)}"
    if not target.exists():
        return target, False
    index = 2
    while True:
        numbered = output_root / f"{target.name}-{index}"
        if not numbered.exists():
            return numbered, False
        index += 1


def publish_atomic(stage_bundle: Path, target: Path, replacing: bool) -> None:
    if not replacing:
        os.replace(stage_bundle, target)
        return
    backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
    os.replace(target, backup)
    try:
        os.replace(stage_bundle, target)
    except Exception:
        os.replace(backup, target)
        raise
    else:
        shutil.rmtree(backup)


def convert(args: argparse.Namespace) -> dict:
    requested_url = validate_public_url(args.url)
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    if args.rendered_html:
        html = load_rendered_html(args.rendered_html.resolve())
        final_url = requested_url
        rendered = True
    else:
        html, final_url = fetch_html(session, requested_url)
        final_url = validate_public_url(final_url)
        rendered = False

    soup = BeautifulSoup(html, "lxml")
    if soup.find("input", attrs={"type": re.compile(r"^password$", re.I)}):
        raise ConversionError("The page appears to require authentication")
    root = find_content_root(soup)
    root_text = tag_text(root)
    paragraph_count = len(root.find_all("p"))
    if (
        len(root_text) < MIN_ARTICLE_CHARACTERS
        or (paragraph_count == 0 and len(root_text) < 1000)
        or PLACEHOLDER_RE.search(root_text)
    ):
        if not rendered:
            raise RenderRequired(
                "The direct HTML does not contain enough rendered article content"
            )
        if ACCESS_CONTROL_RE.search(root_text):
            raise ConversionError(
                "The rendered page appears to require authentication or payment"
            )
        raise ConversionError("Rendered page still has insufficient article content")

    metadata = extract_metadata(soup, root, final_url)
    title_fallback = domain_slug(metadata["canonical"])
    title_slug = slugify(metadata["title"], fallback=title_fallback)
    target, replacing = choose_target(
        output_root,
        title_slug=title_slug,
        canonical=metadata["canonical"],
    )
    bundle_name = target.name

    hero = find_external_hero(soup, root, metadata, final_url)
    _, content = clone_content(root, hero)
    remove_boilerplate(content)
    convert_inline_svgs(content)
    add_annotated_background_images(content)
    prepare_pictures(content)
    remove_duplicate_title(content, metadata["title"])
    sanitize_links(content, metadata["canonical"])

    with tempfile.TemporaryDirectory(
        prefix=".webpage-to-local-markdown-",
        dir=str(output_root),
    ) as temporary:
        temporary_root = Path(temporary)
        stage_bundle = temporary_root / bundle_name
        stage_bundle.mkdir()
        images_dir = stage_bundle / "images"
        try:
            image_count = process_images(
                content,
                session=session,
                base_url=final_url,
                images_dir=images_dir,
            )
        except RenderRequired:
            if not rendered:
                raise
            raise ConversionError("Rendered page contains an unresolved content image")
        normalize_figures(content)
        simplify_tables(content)
        strip_attributes_and_wrappers(content)
        document = build_clean_document(content, metadata)
        clean_html = temporary_root / "clean-source.html"
        clean_html.write_text(str(document), encoding="utf-8")

        markdown_path = stage_bundle / f"{bundle_name}.md"
        run_pandoc(clean_html, markdown_path)
        validation = validate_bundle(stage_bundle, source_html=clean_html)
        publish_atomic(stage_bundle, target, replacing)

    return {
        "status": "ok",
        "bundle": str(target),
        "markdown": str(target / f"{bundle_name}.md"),
        "images": image_count,
        "source_url": metadata["canonical"],
        "rendered_html_used": rendered,
        "updated_existing_source": replacing,
        "validated_tokens": validation["markdown_tokens"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Public http/https webpage URL")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path.cwd(),
        help="Destination root; defaults to the current working directory",
    )
    parser.add_argument(
        "--rendered-html",
        type=Path,
        help="Temporary browser-rendered HTML capture for JavaScript pages",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = convert(args)
    except RenderRequired as exc:
        emit(
            {
                "status": "render_required",
                "reason": str(exc),
                "url": args.url,
            }
        )
        return RENDER_REQUIRED_EXIT
    except (
        ConversionError,
        ValidationError,
        OSError,
        requests.RequestException,
    ) as exc:
        emit({"status": "error", "error": str(exc)}, stream=sys.stderr)
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
