---
name: translate-markdown-to-zh-cn
description: Translate a local English Markdown file or offline Markdown article bundle into a complete, faithful Simplified Chinese sibling while preserving Markdown structure, code, URLs, images, and resource links. Use this skill whenever the user asks to “翻译 Markdown/MD 为中文”, “生成中文版 md”, “将英文文章翻成简中”, “保证完整性和资源链接正确”, “信雅达翻译并保留技术术语”, or otherwise wants a local Markdown article translated to zh-CN, even if they only point to a directory rather than a specific file. Do not use for summaries, webpage downloading, PDF/Word translation, or translation into languages other than Simplified Chinese.
compatibility: Requires Python 3.10+; the bundled validator uses only the Python standard library.
---

# Translate Markdown to Simplified Chinese

Translate one local English Markdown document into a faithful Simplified
Chinese sibling. Preserve the source file and every accompanying resource.
The translation should read as professional Chinese while transmitting the
original meaning, emphasis, and level of certainty precisely.

## Output contract

By default, create the translation beside the source:

```text
article/
├── article.md
├── article.zh-CN.md
└── images/
    └── ...
```

Use `<source-stem>.zh-CN.md` unless the user explicitly requests another name.
Do not copy, rename, regenerate, or edit image and other resource files.

When the default target already exists, update it only when it clearly belongs
to the selected source. A matching canonical `Source` URL is strong evidence;
a matching structure and resource/link signature is also sufficient. If the
existing file cannot be associated safely, ask before overwriting it.

## Resolve the source

Accept either a `.md` file or a directory.

For a directory:

1. Prefer `<directory-name>/<directory-name>.md` when it exists and is not a
   localized file.
2. Otherwise, ignore `*.zh-CN.md` and select the only remaining Markdown file.
3. If no candidate exists, report that no English Markdown source was found.
4. If multiple candidates remain and none is clearly primary, ask the user to
   identify the source instead of guessing.

Read the entire source before translating. Inventory its headings, separated
content blocks, lists, block quotes, tables, code, links, image references,
footnotes, and local resources so omissions are visible during review.

## Translation principles

Translate every reader-facing part of the article:

- title and headings;
- metadata labels and prose values;
- abstract, body paragraphs, block quotes, lists, and tables;
- link labels, image alt text, figure captions, and callouts;
- footnotes, acknowledgements, appendices, and related-content labels.

Keep one translated counterpart for every source content block. Do not
summarize, omit, merge away, invent, fact-check into a different claim, or add
commentary that was not in the source. Preserve the original argument,
qualification, tone, formatting emphasis, and paragraph order.

Write idiomatic, polished Simplified Chinese rather than following English
word order mechanically. Favor accuracy first, then clarity and elegance.
Resolve pronouns and long English sentences naturally without weakening or
strengthening the author's claim.

## Terminology

At the first meaningful occurrence of a specialized term, normally use a
concise Chinese rendering followed by the original term, for example
`评分器（grader）` or `评估框架（evaluation harness）`. Reuse the chosen form
consistently afterward.

Keep the original form for proper names and tokens whose identity matters:

- people, organizations, products, models, libraries, protocols, APIs, and
  standards;
- benchmark and dataset names;
- metric names such as `pass@k` and `pass^k`;
- CLI flags, file paths, configuration keys, code identifiers, and literal
  values.

Use judgment for established Chinese technical terms. Retaining the English
term should improve precision, not leave ordinary prose needlessly
untranslated.

## Markdown and link preservation

Preserve the source Markdown organization and equivalent formatting:

- retain heading levels and order;
- retain the number and order of separated content blocks, list items, quote
  lines, table rows, and fenced code blocks;
- keep emphasis attached to the equivalent translated phrase;
- keep fenced code block bodies, fence info strings, inline code, formulas,
  escaped literals, and code-like identifiers byte-for-byte unchanged;
- keep external URLs, image destinations, and non-image relative file
  destinations unchanged;
- translate visible link labels and image alt text when they are prose;
- keep footnote identifiers and references paired correctly.

Fragment-only links such as `#footnotes` are the exception to unchanged link
destinations. When their target heading is translated, update the fragment to
the translated GitHub-style heading slug and verify that it resolves. Preserve
links to fragments in other files because those headings were not translated.

Do not convert local images into remote links or absolute paths. Keep their
existing relative paths so the Chinese file works from the same directory as
the source.

## Workflow

1. Resolve and read the full source using the rules above.
2. Establish a small, document-specific terminology glossary before drafting.
3. Translate in source order, preserving one-to-one block correspondence.
4. Write only the target Markdown file; leave the source and resources intact.
5. Run the bundled validator:

   ```bash
   python "<skill-dir>/scripts/validate_translation.py" \
     "/absolute/path/article.md" \
     "/absolute/path/article.zh-CN.md"
   ```

6. Read every reported error, repair the translation, and rerun validation
   until it returns `status: "ok"`.
7. Perform a final language pass for meaning, fluency, terminology consistency,
   untranslated prose, and accidental additions. Deterministic validation
   proves structural fidelity, but it does not replace editorial judgment.
8. Return a clickable link to the translated Markdown and summarize the
   structural/resource checks that passed.

Never report the translation as complete when validation fails. If a source
construct falls outside the validator's supported Markdown forms, inspect it
manually and explain the limitation rather than weakening the content to make
the check pass.
