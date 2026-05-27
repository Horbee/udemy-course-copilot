#!/usr/bin/env python3
"""
Udemy Transcript Extractor

Parses Udemy transcript HTML files and extracts readable plain-text transcripts.
Runs in batch mode over an input directory and writes results to an output directory.
"""

import argparse
import sys
import textwrap
from pathlib import Path

from bs4 import BeautifulSoup


def find_project_root() -> Path:
    """Find the project root by looking for pyproject.toml."""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return current


def extract_transcript(html_path: Path) -> tuple[str, int]:
    """Extract transcript text from a Udemy HTML file.

    Returns:
        A tuple of (raw text, number of cues extracted).
    """
    with html_path.open("r", encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")

    panel = soup.find("div", {"data-purpose": "transcript-panel"})
    if not panel:
        raise ValueError("No transcript panel found in HTML file.")

    cues = panel.find_all("span", {"data-purpose": "cue-text"})
    if not cues:
        raise ValueError("No transcript cues found in panel.")

    parts = []
    for cue in cues:
        text = cue.get_text(separator=" ", strip=True)
        if text:
            parts.append(text)

    full_text = " ".join(parts)
    # Normalize any remaining multi-space/newline artefacts from HTML formatting
    full_text = " ".join(full_text.split())
    return full_text, len(cues)


def format_transcript(text: str, width: int = 80) -> str:
    """Wrap transcript text to a readable line width."""
    paragraphs = text.split("\n\n")
    wrapped_paragraphs = [
        textwrap.fill(
            p.strip(),
            width=width,
            break_long_words=False,
            replace_whitespace=True,
        )
        for p in paragraphs
        if p.strip()
    ]
    return "\n\n".join(wrapped_paragraphs)


def process_file(html_path: Path, output_dir: Path, width: int) -> bool:
    """Process a single HTML file. Returns True on success, False on failure."""
    try:
        raw_text, cue_count = extract_transcript(html_path)
        formatted_text = format_transcript(raw_text, width=width)
    except ValueError as exc:
        print(f"  [ERROR] {html_path.name}: {exc}", file=sys.stderr)
        return False

    output_path = output_dir / html_path.with_suffix(".txt").name
    output_path.write_text(formatted_text, encoding="utf-8")
    print(f"  {cue_count} cues  ->  {output_path.relative_to(Path.cwd())}")
    return True


def main() -> None:
    project_root = find_project_root()

    parser = argparse.ArgumentParser(
        description="Extract readable transcripts from Udemy HTML files."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=project_root / "transcripts",
        help="Directory containing HTML transcript files (default: transcripts/)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=project_root / "outputs",
        help="Directory to write .txt files (default: outputs/)",
    )
    parser.add_argument(
        "-w",
        "--width",
        type=int,
        default=80,
        help="Maximum line width for wrapping (default: 80)",
    )
    args = parser.parse_args()

    # Ensure directories exist
    args.input_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    html_files = sorted(args.input_dir.glob("*.html"))
    if not html_files:
        print(f"No .html files found in {args.input_dir}")
        sys.exit(0)

    print(f"Processing {len(html_files)} file(s) from {args.input_dir}...")
    success_count = 0
    skip_count = 0

    for html_path in html_files:
        if process_file(html_path, args.output_dir, args.width):
            success_count += 1
        else:
            skip_count += 1

    print(
        f"\nSummary: {success_count} file(s) processed successfully, "
        f"{skip_count} file(s) skipped, {len(html_files)} total found"
    )


if __name__ == "__main__":
    main()
