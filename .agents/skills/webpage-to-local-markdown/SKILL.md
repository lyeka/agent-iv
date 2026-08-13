---
name: webpage-to-local-markdown
description: Convert a user-supplied public webpage URL into a complete local Markdown bundle containing the full main text and locally downloaded content images. Use this skill whenever the user asks to save, archive, download, or export a webpage, article, blog post, or documentation page as Markdown/MD, offline Markdown, or “正文和图片保存到本地”, even if they only say “保存这个 URL”. Do not use for summary-only requests, PDF/HTML mirroring, whole-site crawling, or bypassing login and paywalls.
compatibility: Requires Python 3.10+, requests, beautifulsoup4, lxml, and Pandoc 3+. The Browser skill is optional and used only as a rendering fallback for public JavaScript pages.
---

# Webpage to Local Markdown

Save one public webpage as a self-contained Markdown bundle. Preserve the
article's language and content; do not summarize, translate, or rewrite it.

## Output contract

Create only Markdown-related output:

```text
<content-derived-name>/
├── <content-derived-name>.md
└── images/
    ├── hero.<ext>
    ├── <caption-or-alt-derived-name>.<ext>
    └── figure-<number>.<ext>
```

Do not keep HTML, CSS, JavaScript, fonts, request dumps, validation reports, or
browser captures in the output directory. Omit `images/` when the page has no
content images.

The Markdown must begin with the page title and human-readable metadata:

```markdown
# Page title

**Source:** [canonical URL](https://example.com/article)

**Author:** Author name

**Published:** Original publication date

**Saved locally:** YYYY-MM-DD
```

Omit unavailable author, publication date, or description fields instead of
guessing them. Keep regular external links, but make every image reference a
local relative path.

## Workflow

1. Resolve the requested output root. Use the current working directory unless
   the user names another directory.
2. Run the deterministic converter:

   ```bash
   python "<skill-dir>/scripts/webpage_to_markdown.py" \
     "<public-http-or-https-url>" \
     --output-root "<output-root>"
   ```

3. Read its JSON result:
   - `status: "ok"` means the bundle was published and validated.
   - `status: "render_required"` with exit code `20` means the direct response
     did not contain enough rendered article content. Continue with the browser
     fallback below.
   - Any other failure means no complete bundle was published. Report the
     concrete reason; do not present a partial result as complete.
4. Return a clickable link to the `.md` file and mention its accompanying
   `images/` directory when present.

The converter stages all work before publishing. When it finds an existing
bundle with the same canonical source URL, it updates that directory
atomically. If a different source produces the same title, it adds the source
domain and then a numeric suffix rather than overwriting unrelated content.

## Browser rendering fallback

Use this only after the converter returns exit code `20`. It is for public
JavaScript-rendered pages, not for bypassing authentication or paywalls.

1. Read and follow the available Browser control skill before taking browser
   actions. Use the browser selected for the supplied URL; do not substitute a
   standalone Playwright installation.
2. Open the URL. If the page requires sign-in, payment, CAPTCHA completion, or
   another access-control step, stop and explain that the skill supports public
   pages only.
3. Create a uniquely named temporary `.html` path outside the output
   directory.
4. In the browser's Node-backed control session, import
   `scripts/capture_rendered_page.mjs` by absolute path and call:

   ```js
   await captureRenderedPage(tab, "/absolute/temp/page.html")
   ```

   The helper scrolls through the page to trigger lazy loading, annotates
   meaningful content background images, checks for access-control UI, and
   writes the rendered DOM directly to the temporary file.
5. If the helper returns `blocked: true`, stop without converting.
6. Rerun the converter with the rendered capture:

   ```bash
   python "<skill-dir>/scripts/webpage_to_markdown.py" \
     "<public-http-or-https-url>" \
     --rendered-html "/absolute/temp/page.html" \
     --output-root "<output-root>"
   ```

7. Delete only that exact temporary file after the converter finishes,
   whether it succeeds or fails.

## Content rules

- Prefer semantic article containers, then common article-body containers,
  then the largest text-heavy main-content block.
- Keep headings, paragraphs, emphasis, lists, block quotes, links, footnotes,
  tables, fenced code blocks, figures, and captions.
- Remove site navigation, footer links, advertisements, cookie notices,
  sharing controls, recommendations, comments, copy buttons, tracking pixels,
  and decorative interface icons.
- Download the best available image candidate from `srcset`, lazy-loading
  attributes, Next.js image proxy URLs, data URIs, meaningful inline SVGs, and
  browser-annotated content backgrounds.
- Derive names from the page title, figure caption, or image alt text while
  preserving useful Unicode such as Chinese. Use `hero` or `figure-N` only when
  no more descriptive name exists.
- Convert complex tables to Markdown pipe tables. Flatten multi-block table
  cells with semicolons so the final `.md` contains no raw HTML tags.
- Treat an unresolved content image or lost body text as a conversion failure.

## Validation

The converter automatically runs `scripts/validate_markdown_bundle.py` before
publishing. For manual revalidation:

```bash
python "<skill-dir>/scripts/validate_markdown_bundle.py" \
  "/path/to/content-bundle"
```

Validation requires one Markdown file, only local image references, valid image
files, no raw HTML blocks, and no unexpected output resources. During
conversion it also compares the cleaned source and generated Markdown Pandoc
AST text tokens so body text cannot disappear silently.

