#!/usr/bin/env python3
"""
Obsidian Note Generator

Takes a single lecture transcript (.txt) and, optionally, its slide deck
(.pdf), sends both to the OpenAI API to produce a clean Obsidian-flavoured
Markdown study note, and appends a foldable (toggleable) Mermaid mind map
summarising the lecture's topic hierarchy.

Usage:
    uv run scripts/obsidian_note.py --transcript outputs/01_001_..._intro.txt
    uv run scripts/obsidian_note.py \
        --transcript outputs/01_001_..._intro.txt \
        --slides slides/01_intro.pdf \
        --model gpt-4o-mini

    uv run scripts/obsidian_note.py --transcript outputs/day_1.txt --dry-run
    uv run scripts/obsidian_note.py --transcript outputs/day_1.txt --no-mindmap
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
# Prompts
# ---------------------------------------------------------------------------

NOTES_SYSTEM_PROMPT = r"""
Create a study-material assistant that takes lecture transcripts (and, when attached, the lecture's
slide deck PDF itself) supplied by the user and extracts the most important information into clean,
useful study notes formatted in Markdown for Obsidian. When a slide deck PDF is attached, read its
native layout, text, diagrams, and images directly. Preserve essential
facts, concepts, definitions, arguments, examples, relationships, dates, names, formulas, and
action points while removing repetition, filler, and conversational noise. Structure notes with
clear Markdown headings, concise bullet points, nested bullets where useful, and Obsidian-friendly
conventions such as [[wiki links]] for important concepts, **bold** for key terms, `inline code`
for literal terms or commands, blockquotes only for especially important verbatim statements, and
optional callouts such as > [!summary], > [!definition], > [!example], and > [!warning] when they
improve study value. For any mathematical formulas, use Obsidian's native LaTeX math delimiters:
`$...$` for inline math and `$$...$$` on their own lines for block/display math. Never use
`\(...\)` or `\[...\]` delimiters — Obsidian does not render those. Include a concise summary near
the top, then organized topic sections, key
takeaways, important terms, and — when supported by the transcript — questions for self-testing or
flashcard-style prompts. Do not invent facts that are absent from the transcript or slides; clearly
mark uncertain or ambiguous material. Keep the output optimized for learning, retrieval, and review
rather than producing a line-by-line transcript summary. When the transcript is long or loosely
structured, infer a sensible topic hierarchy without asking unnecessary clarification. Adapt depth
to the material: concise for simple content, more detailed for technical or concept-dense content.

Return ONLY the Markdown body (starting with a `## Summary` heading). Do NOT include YAML
frontmatter or a top-level (`#`) title — that is added separately.
"""

MINDMAP_SYSTEM_PROMPT = """
You are an expert at distilling study notes into a Mermaid.js mind map.

Given Markdown study notes for a single lecture, produce a hierarchical mind map using Mermaid's
`mindmap` diagram syntax that captures the lecture's main topics, subtopics, and key terms.

Rules:
- Output ONLY raw Mermaid code, starting with the `mindmap` keyword on the first line. No
  explanations, no Markdown code fences, no surrounding text.
- Second line must be the root node: two spaces of indent, then `root((Lecture Title))`.
- Indent each deeper level with exactly 2 additional spaces per level — Mermaid's mindmap syntax
  is indentation-sensitive.
- Keep node text short (2-6 words). Avoid parentheses, colons, quotes, or other punctuation that
  can break Mermaid parsing — plain words only.
- Limit the diagram to 3 levels of depth and roughly 20 nodes total so it stays readable.
- Do not invent topics that are not present in the notes.
"""


# ---------------------------------------------------------------------------
# OpenAI helpers
# ---------------------------------------------------------------------------

def _build_openai_client(api_key: str):
    """Instantiate an OpenAI client lazily so the import error is informative."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required. Run: uv add openai"
        ) from exc
    return OpenAI(api_key=api_key)


REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


def _chat(client, model: str, system_prompt: str, user_content: str | list[dict], reasoning_effort: str) -> str:
    def _call():
        return client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            reasoning_effort=reasoning_effort,
        )

    response = _retry_with_backoff(_call, max_retries=3, base_delay=1.0)
    return response.choices[0].message.content.strip()


def generate_notes(
    client, model: str, title: str, transcript_text: str, slides_file_id: str | None, reasoning_effort: str
) -> str:
    """Send the transcript (and optional attached slide PDF) to the OpenAI API and return Markdown notes."""
    parts = [f"# Lecture: {title}", "", "## Transcript", "", transcript_text.strip()]
    if slides_file_id:
        parts += ["", "## Slides", "", "(the lecture's slide deck is attached as a PDF file)"]
    text = "\n".join(parts)

    user_content: str | list[dict]
    if slides_file_id:
        user_content = [
            {"type": "text", "text": text},
            {"type": "file", "file": {"file_id": slides_file_id}},
        ]
    else:
        user_content = text

    return _chat(client, model, NOTES_SYSTEM_PROMPT, user_content, reasoning_effort=reasoning_effort)


def generate_mindmap(client, model: str, title: str, notes_markdown: str, reasoning_effort: str) -> str:
    """Ask the OpenAI API for a Mermaid mindmap summarising the notes. Returns raw Mermaid code."""
    user_content = f"Lecture Title: {title}\n\n## Study Notes\n\n{notes_markdown}"
    raw = _chat(client, model, MINDMAP_SYSTEM_PROMPT, user_content, reasoning_effort=reasoning_effort)
    return _clean_mermaid_code(raw)


def _clean_mermaid_code(raw: str) -> str:
    """Strip stray code fences / labels the model might add despite instructions."""
    lines = [l for l in raw.strip("\n").splitlines() if not l.strip().startswith("```")]
    text = "\n".join(lines).strip("\n")
    if not text.lstrip().startswith("mindmap"):
        text = "mindmap\n" + text
    return text


# ---------------------------------------------------------------------------
# Slide PDF upload
# ---------------------------------------------------------------------------

MAX_SLIDES_BYTES = 32 * 1024 * 1024  # OpenAI's per-request file-input limit


def upload_slides(client, pdf_path: Path) -> str:
    """Upload a slide deck PDF to OpenAI so it can be attached directly to a request.

    The model reads the PDF's native layout, text, diagrams, and images itself, so no local
    text extraction is needed. Requires a vision-capable model (e.g. gpt-4o or newer).
    """
    size = pdf_path.stat().st_size
    if size > MAX_SLIDES_BYTES:
        raise ValueError(
            f"Slides PDF is {size / 1024 / 1024:.1f} MB, exceeding OpenAI's "
            f"{MAX_SLIDES_BYTES / 1024 / 1024:.0f} MB per-request file-input limit."
        )
    with pdf_path.open("rb") as f:
        file_obj = client.files.create(file=f, purpose="user_data")
    return file_obj.id


def cleanup_uploaded_file(client, file_id: str) -> None:
    """Best-effort delete of an uploaded file so it doesn't linger in the OpenAI account."""
    try:
        client.files.delete(file_id)
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  Could not delete uploaded file {file_id}: {exc}")


# ---------------------------------------------------------------------------
# File / metadata helpers
# ---------------------------------------------------------------------------

def find_project_root() -> Path:
    """Find the project root by looking for pyproject.toml."""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return current


def load_metadata(project_root: Path) -> dict[str, Any] | None:
    """Load lecture metadata (full titles, etc.) from outputs/.metadata.json."""
    meta_path = project_root / "outputs" / ".metadata.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _extract_lecture_id(filename: str) -> str | None:
    """Extract the numeric lecture ID from the filename prefix '01_011_52932255_...'."""
    m = re.match(r"^\d{2}_\d{3}_(\d+)_.*", Path(filename).stem)
    return m.group(1) if m else None


def resolve_title(filename: str, meta: dict[str, Any] | None) -> str:
    """Resolve a human-friendly lecture title, preferring outputs/.metadata.json."""
    if meta:
        lecture_id = _extract_lecture_id(filename)
        if lecture_id:
            info = meta.get("lectures", {}).get(str(lecture_id))
            if info and info.get("title"):
                return info["title"].strip()

    base = Path(filename).stem
    cleaned = re.sub(r"^\d{2}_\d{3}_\d+_", "", base)
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    return " ".join(cleaned.split()).strip().title()


def _slug(text: str) -> str:
    sl = re.sub(r"[^\w\s-]", "", text.lower())
    sl = re.sub(r"[\s_]+", "-", sl)
    return sl.strip("-")


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------

def build_mindmap_callout(mermaid_code: str, collapsed: bool = True) -> str:
    """Wrap Mermaid mindmap code in a foldable Obsidian callout, so it's toggleable in the note."""
    marker = "-" if collapsed else "+"
    lines = mermaid_code.strip("\n").splitlines()
    quoted = "\n".join(f"> {l}" if l.strip() else ">" for l in lines)
    return (
        f"> [!example]{marker} 🧠 Mind Map\n"
        f"> ```mermaid\n"
        f"{quoted}\n"
        f"> ```"
    )


@dataclass(frozen=True)
class NoteResult:
    output_path: Path
    has_mindmap: bool
    has_slides: bool


def build_note_markdown(
    title: str,
    notes_markdown: str,
    mindmap_code: str | None,
    transcript_path: Path,
    slides_path: Path | None,
    model: str,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tags = ["llm-course", _slug(title)]
    frontmatter_tags = "\n".join(f"  - {t}" for t in tags)

    frontmatter = f"""---
title: "{title}"
generated_on: "{now}"
model: "{model}"
source_transcript: "{transcript_path.name}"
source_slides: "{slides_path.name if slides_path else 'null'}"
tags:
{frontmatter_tags}
---
"""

    parts = [frontmatter, f"# {title}"]

    if mindmap_code:
        parts.append(build_mindmap_callout(mindmap_code, collapsed=True))

    parts.append(notes_markdown.strip())
    parts.append("")
    parts.append(f"---\n*Source: `{transcript_path.name}`" + (f", `{slides_path.name}`*" if slides_path else "*"))

    return "\n\n".join(p for p in parts if p is not None)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    project_root = find_project_root()

    parser = argparse.ArgumentParser(
        description="Generate an Obsidian Markdown study note (with a toggleable mind map) "
        "from a lecture transcript and, optionally, its slide deck."
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        required=True,
        help="Path to the lecture transcript .txt file",
    )
    parser.add_argument(
        "--slides",
        type=Path,
        default=None,
        help="Path to the lecture's slide deck .pdf file (optional)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_root / "notes",
        help="Output directory for the generated Markdown file (default: notes/)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5.6-sol",
        help="OpenAI model to use (default: gpt-5.6-sol)",
    )
    parser.add_argument(
        "--reasoning",
        type=str,
        choices=REASONING_EFFORTS,
        default="medium",
        help="Reasoning effort for the model (default: medium)",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="OpenAI API key (default: reads from .env OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--no-mindmap",
        action="store_true",
        default=True,
        help="Skip generating the toggleable Mermaid mind map section",
    )
    parser.add_argument(
        "--expand-mindmap",
        action="store_true",
        help="Render the mind map callout expanded by default (still collapsible)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without calling the API",
    )
    args = parser.parse_args()

    if not args.transcript.exists():
        print(f"Error: transcript file not found: {args.transcript}", file=sys.stderr)
        sys.exit(1)
    if args.slides and not args.slides.exists():
        print(f"Error: slides file not found: {args.slides}", file=sys.stderr)
        sys.exit(1)

    meta = load_metadata(project_root)
    title = resolve_title(args.transcript.name, meta)
    output_path = args.output_dir / f"{args.transcript.stem}.md"

    print(f"Transcript: {args.transcript}")
    print(f"Slides:     {args.slides if args.slides else '(none)'}")
    print(f"Title:      {title}")
    print(f"Output:     {output_path}")
    print(f"Mind map:   {'disabled' if args.no_mindmap else 'enabled (collapsed)' if not args.expand_mindmap else 'enabled (expanded)'}")

    if args.dry_run:
        print("\nDry run complete. No API calls were made.")
        sys.exit(0)

    # Resolve API key
    api_key = args.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        try:
            from dotenv import find_dotenv, load_dotenv

            load_dotenv(find_dotenv(usecwd=True))
            api_key = os.getenv("OPENAI_API_KEY")
        except ImportError:
            pass
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

    transcript_text = args.transcript.read_text(encoding="utf-8").strip()
    if not transcript_text:
        print("Error: transcript file is empty.", file=sys.stderr)
        sys.exit(1)

    client = _build_openai_client(api_key)

    slides_file_id: str | None = None
    if args.slides:
        print("\nUploading slide deck to OpenAI...")
        try:
            slides_file_id = upload_slides(client, args.slides)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

    try:
        print(f"\nGenerating study notes with {args.model} (reasoning: {args.reasoning})...")
        try:
            notes_markdown = generate_notes(
                client, args.model, title, transcript_text, slides_file_id, reasoning_effort=args.reasoning
            )
        except RetryError as exc:
            print(f"❌ Failed to generate notes: {exc}", file=sys.stderr)
            sys.exit(1)

        mindmap_code: str | None = None
        if not args.no_mindmap:
            print("Generating mind map...")
            try:
                mindmap_code = generate_mindmap(
                    client, args.model, title, notes_markdown, reasoning_effort=args.reasoning
                )
            except RetryError as exc:
                print(f"  ⚠️  Failed to generate mind map, continuing without it: {exc}")
                mindmap_code = None
    finally:
        if slides_file_id:
            cleanup_uploaded_file(client, slides_file_id)

    note_markdown = build_note_markdown(
        title=title,
        notes_markdown=notes_markdown,
        mindmap_code=None if args.no_mindmap else mindmap_code,
        transcript_path=args.transcript,
        slides_path=args.slides,
        model=args.model,
    )
    if not args.no_mindmap and mindmap_code and args.expand_mindmap:
        note_markdown = note_markdown.replace("> [!example]-", "> [!example]+", 1)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(note_markdown, encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"✅  Note written to: {output_path}")
    print(f"   Mind map:  {'included' if mindmap_code else 'not included'}")
    print(f"   Slides:    {'included' if slides_file_id else 'not included'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
