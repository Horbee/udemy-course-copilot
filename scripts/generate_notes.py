#!/usr/bin/env python3
"""
AI-Powered Study Notes Generator

Reads transcript text files from the outputs/ directory, sends each to the
OpenAI API for enrichment (summary, key concepts, code snippets, extended
knowledge, interview Q&A, flashcards), and compiles the results into a single
Obsidian-compatible Markdown file with YAML frontmatter.

Usage:
    uv run scripts/generate_notes.py --pattern day_1
    uv run scripts/generate_notes.py --pattern day_1 --merge
    uv run scripts/generate_notes.py --pattern day_1 --model gpt-4o-mini
    uv run scripts/generate_notes.py --pattern week_2 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Retry / backoff helpers
# ---------------------------------------------------------------------------

class RetryError(Exception):
    """Raised when all retry attempts are exhausted."""


def _retry_with_backoff(
    fn, max_retries: int = 3, base_delay: float = 1.0, backoff_factor: float = 2.0
):
    """Call *fn*, retrying on transient OpenAI / HTTP errors with exponential backoff."""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            attempt += 1
            if attempt > max_retries:
                raise RetryError(f"Failed after {max_retries} attempts: {exc}") from exc

            # Only retry on known transient errors
            exc_name = type(exc).__name__
            if exc_name not in {
                "APIError",
                "APIConnectionError",
                "RateLimitError",
                "Timeout",
                "ServiceUnavailableError",
                "InternalServerError",
            }:
                raise
            delay = base_delay * (backoff_factor ** (attempt - 1))
            print(f"    ⚠️  Attempt {attempt}/{max_retries} failed ({exc_name}). Retrying in {delay:.1f}s...")
            time.sleep(delay)


# ---------------------------------------------------------------------------
# OpenAI helpers
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a senior technical educator and curriculum designer. Your task is to take the raw transcript of a technical lecture and transform it into a rich, learner-friendly Markdown study note.

## Instructions

1. **Summary**: Write a concise 2-5 sentence summary of what the lecture covers.
2. **Key Concepts**: Extract and explain the most important ideas, definitions, and principles from the transcript. Use Obsidian callout syntax where appropriate:
   - `> [!IMPORTANT]` for definitions / critical rules
   - `> [!TIP]` for practical advice
   - `> [!WARNING]` for common pitfalls
3. **Code Snippets**: If the transcript contains code, configuration, or pseudo-code, present it in a fenced block with the correct language tag. Add a brief explanation of what the code does.
4. **Extended Knowledge**: Expand on the lecture content with deeper context. If you have broader knowledge about the topic, add additional explanations, comparisons to other technologies, theoretical foundations, or industry best practices that were not explicitly mentioned. This section should make the note comprehensive and valuable even after the student completes the course.
5. **Interview Q&A**: Produce 3-5 realistic technical interview-style questions and detailed answers based on the lecture material. These should test understanding, not just memorization.

## Output Format

Return ONLY valid Markdown. Use the following exact section headers (with `##` and `###` as shown):

```markdown
### Summary
...

### Key Concepts
> [!IMPORTANT] Concept Name
> ...

...

### Code Snippets
```language
...
```
...

### Extended Knowledge
...

### Interview Q&A
**Q1:** ...
**A1:** ...

...
```

Do NOT include YAML frontmatter, a top-level title, or introductory/concluding sentences outside the sections.
"""


MERGED_SYSTEM_PROMPT = SYSTEM_PROMPT + """

## Merged Transcript Mode

The user may provide several lecture transcripts concatenated together with source labels.
Treat them as one larger lesson and produce one cohesive study note, not one note per
lecture. Synthesize repeated ideas, organize the material by concept, and avoid section
headers for individual source files.
"""


def _build_openai_client(api_key: str):
    """Instantiate an OpenAI client lazily so the import error is informative."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required. Run: uv add openai"
        ) from exc
    return OpenAI(api_key=api_key)


def generate_notes_for_lecture(
    client,
    model: str,
    transcript_text: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    """Send a single lecture transcript to the OpenAI API and return the Markdown response."""
    def _call():
        return client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript_text},
            ],
            temperature=0.4,
            # max_tokens=4096,
        )

    response = _retry_with_backoff(_call, max_retries=3, base_delay=1.0)
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def find_project_root() -> Path:
    """Find the project root by looking for pyproject.toml."""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return current


def discover_transcripts(project_root: Path, pattern: str) -> list[Path]:
    """Find transcript files in outputs/ matching the given regex pattern."""
    outputs_dir = project_root / "outputs"
    if not outputs_dir.exists():
        raise FileNotFoundError(f"Outputs directory not found: {outputs_dir}")

    try:
        filename_re = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern '{pattern}': {exc}") from exc

    matched = [
        p for p in outputs_dir.glob("*.txt") if filename_re.search(p.name)
    ]
    # Sort by filename to preserve curriculum order
    matched.sort(key=lambda p: p.name)
    return matched


def load_metadata(project_root: Path) -> dict[str, Any] | None:
    """Load lecture metadata (full titles, etc.) from outputs/.metadata.json."""
    meta_path = project_root / "outputs" / ".metadata.json"
    if not meta_path.exists():
        return None
    try:
        text = meta_path.read_text(encoding="utf-8")
        return json.loads(text)
    except (json.JSONDecodeError, OSError):
        return None


def _extract_lecture_id(filename: str) -> str | None:
    """Extract the numeric lecture ID from the filename prefix '01_011_52932255_...'."""
    stem = Path(filename).stem
    m = re.match(r"^\d{2}_\d{3}_(\d+)_.*", stem)
    return m.group(1) if m else None


def _extract_display_title(filename: str, meta: dict[str, Any] | None) -> str:
    """Convert a raw filename into a human-friendly lecture title.

    If the metadata JSON contains a full title for this lecture_id, use that.
    Otherwise fall back to the truncated filename-based title.
    """
    if meta:
        lecture_id = _extract_lecture_id(filename)
        if lecture_id:
            meta_lectures = meta.get("lectures", {})
            info = meta_lectures.get(str(lecture_id))
            if info and info.get("title"):
                return info["title"].strip()

    # Fallback: parse the truncated filename
    base = Path(filename).stem
    cleaned = re.sub(r"^\d{2}_\d{3}_\d+_", "", base)
    cleaned = cleaned.replace("_", " ")
    return cleaned.strip().title()


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """Create an anchor-friendly slug."""
    sl = re.sub(r"[^\w\s-]", "", text.lower())
    sl = re.sub(r"[\s_]+", "-", sl)
    return sl.strip("-")


def _build_toc(note_blocks: list["NoteBlock"]) -> str:
    lines = ["\n## Table of Contents\n"]
    for title, _, _ in note_blocks:
        anchor = _slug(title)
        lines.append(f"- [{title}](#{anchor})")
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class CompileResult:
    output_path: Path
    success_count: int
    skip_count: int


@dataclass(frozen=True)
class SourceEntry:
    index: int
    title: str
    path: Path


NoteBlock = tuple[str, str, SourceEntry | None]


def compile_grouped_markdown(
    pattern: str,
    note_blocks: list[NoteBlock],
    source_entries: list[SourceEntry],
    model: str,
    output_dir: Path,
    merged: bool = False,
) -> CompileResult:
    """Assemble all per-lecture notes into one Markdown file with YAML frontmatter."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{pattern.lower()}.md"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    safe_tag = re.sub(r"[^\w]", "-", pattern.lower())
    frontmatter = f"""---
title: "{pattern.replace('_', ' ').title()} – Study Guide"
generated_on: "{now}"
model: "{model}"
source_files: {len(source_entries)}
merged: {str(merged).lower()}
tags:
  - llm-course
  - {safe_tag}
---
"""

    toc = _build_toc(note_blocks)

    parts = [frontmatter]
    parts.append(f"# {pattern.replace('_', ' ').title()} – Study Guide")
    parts.append(
        f"> [!INFO] Overview\n> This study guide was generated from "
        f"{len(source_entries)} lecture transcripts using OpenAI's `{model}`.\n"
    )
    parts.append(_build_sources(source_entries))
    parts.append(toc)

    success_count = 0
    skip_count = 0

    for title, content, source_entry in note_blocks:
        if not content:
            # Lecture was skipped
            parts.append(f"---\n\n## {title}")
            parts.append(
                "> [!WARNING] Lecture skipped\n> This lecture could not be "
                "processed (API error or empty response). You may want to "
                "generate it manually or retry.\n"
            )
            skip_count += 1
            continue

        anchor = _slug(title)
        parts.append(f"---\n\n## {title}\n<a id=\"{anchor}\"></a>")

        if source_entry:
            parts.append(f"*Source: [S{source_entry.index}]*\n")
        parts.append(content)
        parts.append("")
        success_count += 1

    out_path.write_text("\n".join(parts), encoding="utf-8")
    return CompileResult(output_path=out_path, success_count=success_count, skip_count=skip_count)


def _build_sources(source_entries: list[SourceEntry]) -> str:
    lines = ["\n## Sources\n"]
    for source in source_entries:
        lines.append(f"- [S{source.index}] `outputs/{source.path.name}` - {source.title}")
    lines.append("")
    return "\n".join(lines)


def _build_merged_transcript(source_entries: list[SourceEntry]) -> tuple[str, list[SourceEntry]]:
    chunks: list[str] = []
    empty_sources: list[SourceEntry] = []

    for source in source_entries:
        transcript = source.path.read_text(encoding="utf-8").strip()
        if not transcript:
            empty_sources.append(source)
            continue

        chunks.append(
            "\n".join(
                [
                    f"### Source [S{source.index}]: {source.title}",
                    f"File: outputs/{source.path.name}",
                    "",
                    transcript,
                ]
            )
        )

    return "\n\n---\n\n".join(chunks), empty_sources


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    project_root = find_project_root()

    parser = argparse.ArgumentParser(
        description="Generate AI-enhanced study notes from transcript files."
    )
    parser.add_argument(
        "--pattern",
        "-p",
        type=str,
        required=True,
        help="Regex pattern to match transcript filenames (e.g. 'day_1', 'day_2|day_3')",
    )
    parser.add_argument(
        "--model",
        "-m",
        type=str,
        default="gpt-5.4-mini",
        help="OpenAI model to use (default: gpt-5.4-mini)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_root / "notes",
        help="Output directory for generated Markdown files (default: notes/)",
    )
    parser.add_argument(
        "--api-key",
        "-k",
        type=str,
        default=None,
        help="OpenAI API key (default: reads from .env OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--merge",
        "-mg",
        action="store_true",
        help="Merge all matched transcripts into one OpenAI request and one generated note.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which files would be matched without calling the API",
    )
    args = parser.parse_args()

    # Resolve env vars for API key
    api_key = args.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        # Try loading from .env
        try:
            from dotenv import find_dotenv, load_dotenv

            load_dotenv(find_dotenv(usecwd=True))
            api_key = os.getenv("OPENAI_API_KEY")
        except ImportError:
            pass

    # Discover files BEFORE checking the API key so --dry-run works without one
    try:
        matched = discover_transcripts(project_root, args.pattern)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not matched:
        print(f"No transcript files matched the pattern '{args.pattern}' in outputs/.")
        sys.exit(0)

    print(f"Matched {len(matched)} transcript(s) for pattern '{args.pattern}':")
    for p in matched:
        print(f"  • {p.name}")

    if args.dry_run:
        print("\nDry run complete. No API calls were made.")
        sys.exit(0)

    if not api_key:
        print(
            "Error: OPENAI_API_KEY not found.\n"
            "  1. Create a .env file in the project root and add:\n"
            "     OPENAI_API_KEY=sk-...\n"
            "  2. Or pass --api-key sk-...\n"
            "  3. Or export OPENAI_API_KEY=sk-...\n",
            file=sys.stderr,
        )
        sys.exit(1)

    client = _build_openai_client(api_key)

    # Load metadata (if available) for full, untruncated lecture titles
    meta = load_metadata(project_root)
    if meta is None:
        print("ℹ️  No .metadata.json found. Full titles require re-running fetch_transcripts.py.")

    source_entries = [
        SourceEntry(
            index=i,
            title=_extract_display_title(path.name, meta),
            path=path,
        )
        for i, path in enumerate(matched, start=1)
    ]

    note_blocks: list[NoteBlock] = []
    sleep_between = 0.4  # seconds between API calls

    total = len(matched)
    if args.merge:
        print(f"\nMerging {total} lecture(s) into one OpenAI request ({args.model})...\n")
        merged_transcript, empty_sources = _build_merged_transcript(source_entries)
        for source in empty_sources:
            print(f"  ⚠️  Skipping empty transcript: {source.title}")

        merged_title = f"{args.pattern.replace('_', ' ').title()} Notes"
        if not merged_transcript.strip():
            print("Error: all matched transcripts are empty.", file=sys.stderr)
            sys.exit(1)

        try:
            md = generate_notes_for_lecture(
                client,
                args.model,
                merged_transcript,
                system_prompt=MERGED_SYSTEM_PROMPT,
            )
        except RetryError as exc:
            print(f"    ❌ Failed after retries: {exc}")
            note_blocks.append((merged_title, "", None))
        else:
            note_blocks.append((merged_title, md, None))
    else:
        print(f"\nSending {total} lecture(s) to OpenAI ({args.model})...\n")

        for i, source in enumerate(source_entries, start=1):
            print(f"  [{i}/{total}] {source.title}")

            transcript = source.path.read_text(encoding="utf-8")
            if not transcript.strip():
                print("    ⚠️  Skipping empty transcript.")
                note_blocks.append((source.title, "", source))
                continue

            try:
                md = generate_notes_for_lecture(client, args.model, transcript)
            except RetryError as exc:
                print(f"    ❌ Failed after retries: {exc}")
                note_blocks.append((source.title, "", source))
                continue

            note_blocks.append((source.title, md, source))
            if i < total:
                time.sleep(sleep_between)

    # Compile
    result = compile_grouped_markdown(
        pattern=args.pattern,
        note_blocks=note_blocks,
        source_entries=source_entries,
        model=args.model,
        output_dir=args.output_dir,
        merged=args.merge,
    )

    print(f"\n{'='*60}")
    print(f"✅  Notes written to: {result.output_path}")
    if args.merge:
        print(f"   Notes generated:     {result.success_count}")
        print(f"   Notes skipped:       {result.skip_count}")
        print(f"   Source transcripts:  {total}")
    else:
        print(f"   Lectures included:   {result.success_count}")
        print(f"   Lectures skipped:    {result.skip_count}")
        print(f"   Total transcripts:   {total}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
