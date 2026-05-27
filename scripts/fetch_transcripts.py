#!/usr/bin/env python3
"""
Udemy Transcript Fetcher

Fetches course curriculum via Udemy's internal APIs, downloads VTT captions,
and converts them to readable plain-text transcripts.

Usage:
    uv run scripts/fetch_transcripts.py                    # Interactive mode
    uv run scripts/fetch_transcripts.py --course-id 6100015 --sections all
    uv run scripts/fetch_transcripts.py --cookies www_udemy_com_cookies.json

Requires:
    - A valid Udemy session cookie export (JSON format, e.g. from a browser extension)
    - Active Udemy subscription to the target course
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def find_project_root() -> Path:
    """Find the project root by looking for pyproject.toml."""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return current


def load_cookies(path: Path) -> dict[str, str]:
    """Load cookies from a JSON file exported by Playwright."""
    with path.open("r", encoding="utf-8") as f:
        cookies = json.load(f)
    return {c["name"]: c["value"] for c in cookies}


def get_last_course_id(project_root: Path) -> str:
    """Load the last used course ID from tmp/."""
    tmp_path = project_root / ".tmp" / "last_course_id.txt"
    if tmp_path.exists():
        return tmp_path.read_text(encoding="utf-8").strip()
    return ""


def save_last_course_id(project_root: Path, course_id: str) -> None:
    """Save the current course ID to .tmp/ for next run."""
    tmp_path = project_root / ".tmp" / "last_course_id.txt"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(course_id, encoding="utf-8")


def create_client(cookies: dict[str, str]) -> httpx.Client:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }
    return httpx.Client(
        cookies=cookies,
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
    )


def test_auth(client: httpx.Client) -> bool:
    """Quick auth check using a lightweight endpoint."""
    resp = client.get("https://www.udemy.com/api-2.0/contexts/me/")
    return resp.status_code == 200


def fetch_curriculum(client: httpx.Client, course_id: str) -> list[dict[str, Any]]:
    """Fetch all curriculum items for a course, following pagination."""
    all_items: list[dict[str, Any]] = []
    url = (
        f"https://www.udemy.com/api-2.0/courses/{course_id}/subscriber-curriculum-items/"
        "?curriculum_types=chapter,lecture,practice,quiz,role-play"
        "&page_size=200"
    )
    while url:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
        all_items.extend(data.get("results", []))
        url = data.get("next")
    return all_items


def group_by_section(items: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group curriculum items into (section_title, lectures) tuples."""
    sections: list[tuple[str, list[dict[str, Any]]]] = []
    current_title = "Unknown Section"
    current_lectures: list[dict[str, Any]] = []

    # Sort by sort_order to guarantee correct curriculum order
    sorted_items = sorted(items, key=lambda x: x.get("sort_order", 0), reverse=True)

    for item in sorted_items:
        item_class = item.get("_class")
        if item_class == "chapter":
            if current_lectures or sections:
                sections.append((current_title, current_lectures))
            current_title = item.get("title", "Untitled Section")
            current_lectures = []
        elif item_class == "lecture":
            current_lectures.append(item)

    if current_lectures:
        sections.append((current_title, current_lectures))

    return sections


def _build_metadata(
    sections: list[tuple[str, list[dict[str, Any]]]], course_id: str
) -> dict[str, Any]:
    """Build a metadata map from the curriculum structure (lecture_id → full info)."""
    lectures: dict[str, dict[str, Any]] = {}
    for section_idx, (section_title, lectures_list) in enumerate(sections, start=1):
        for lecture_idx, lecture in enumerate(lectures_list, start=1):
            lecture_id = str(lecture.get("id"))
            lectures[lecture_id] = {
                "title": lecture.get("title", "Untitled"),
                "section_title": section_title,
                "section_index": section_idx,
                "lecture_index": lecture_idx,
            }
    return {
        "course_id": course_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lectures": lectures,
    }


def write_metadata(
    sections: list[tuple[str, list[dict[str, Any]]]], course_id: str, outputs_dir: Path
) -> None:
    """Write lecture metadata to outputs/.metadata.json so downstream tools can resolve full titles."""
    meta = _build_metadata(sections, course_id)
    meta_path = outputs_dir / ".metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Metadata written: {meta_path}")


def fetch_lecture_detail(client: httpx.Client, course_id: str, lecture_id: int) -> dict[str, Any]:
    """Fetch lecture metadata including caption URLs."""
    url = (
        f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/lectures/{lecture_id}/"
        "?fields[lecture]=asset,description,download_url,is_free,last_watched_second"
        "&fields[asset]=asset_type,length,media_license_token,course_is_drmed,"
        "media_sources,captions,thumbnail_sprite,slides,slide_urls,download_urls,external_url"
    )
    resp = client.get(url)
    resp.raise_for_status()
    return resp.json()


def pick_english_caption(captions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select the best English caption from available options.

    TODO: Add multi-language support. Currently defaults to English (en_US).
    To implement: prompt user for locale, or download all available locales.
    """
    for c in captions:
        locale = c.get("locale_id", "").lower()
        if locale == "en_us":
            return c
    for c in captions:
        locale = c.get("locale_id", "").lower()
        if locale == "en":
            return c
    for c in captions:
        locale = c.get("locale_id", "").lower()
        if locale.startswith("en"):
            return c
    return None


def parse_vtt(vtt_text: str) -> str:
    """Convert a WebVTT subtitle file into a single plain-text string."""
    cues: list[str] = []
    current_lines: list[str] = []

    for line in vtt_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "WEBVTT" or stripped.startswith("NOTE"):
            if current_lines:
                cues.append(" ".join(current_lines))
                current_lines = []
            continue
        if "--" in stripped or re.match(r"^\d{2}:\d{2}[:.]", stripped):
            if current_lines:
                cues.append(" ".join(current_lines))
                current_lines = []
            continue
        cleaned = re.sub(r"</?[^>]+>", "", stripped)
        if cleaned:
            current_lines.append(cleaned)

    if current_lines:
        cues.append(" ".join(current_lines))

    return " ".join(cues)


def safe_filename(text: str) -> str:
    """Sanitise a string for use as a file name."""
    return re.sub(r"[^\w\s-]", "", text).strip().replace(" ", "_").lower()[:40]


def process_lecture(
    client: httpx.Client,
    course_id: str,
    lecture: dict[str, Any],
    section_idx: int,
    lecture_idx: int,
    vtt_cache_dir: Path,
    outputs_dir: Path,
    text_width: int,
    delay: float,
) -> tuple[bool, str]:
    """Download VTT for a single lecture, parse it, and write .txt.

    Returns (success, reason_or_empty).
    """
    lecture_id = lecture.get("id")
    title = lecture.get("title", "Untitled Lecture")
    safe_title = safe_filename(title)
    prefix = f"{section_idx:02d}_{lecture_idx:03d}_{lecture_id}_{safe_title}"

    try:
        detail = fetch_lecture_detail(client, course_id, lecture_id)
    except httpx.HTTPStatusError as exc:
        return False, f"API error {exc.response.status_code}"

    asset = detail.get("asset", {})
    captions = asset.get("captions", [])
    if not captions:
        return False, "no captions"

    caption = pick_english_caption(captions)
    if not caption:
        return False, "no English caption"

    vtt_url = caption.get("url")
    if not vtt_url:
        return False, "caption URL missing"

    try:
        vtt_resp = client.get(vtt_url)
        vtt_resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return False, f"VTT download error {exc.response.status_code}"

    vtt_text = vtt_resp.text
    if not vtt_text.strip():
        return False, "empty VTT"

    # Save raw VTT
    vtt_path = vtt_cache_dir / f"{prefix}.vtt"
    vtt_path.write_text(vtt_text, encoding="utf-8")

    # Parse to text and wrap
    raw_text = parse_vtt(vtt_text)
    if not raw_text:
        return False, "VTT parsing empty"

    formatted = textwrap.fill(
        raw_text,
        width=text_width,
        break_long_words=False,
        replace_whitespace=True,
    )

    txt_path = outputs_dir / f"{prefix}.txt"
    txt_path.write_text(formatted, encoding="utf-8")

    time.sleep(delay)
    return True, ""


def main() -> None:
    project_root = find_project_root()

    parser = argparse.ArgumentParser(description="Fetch Udemy transcripts via API.")
    parser.add_argument("--course-id", type=str, help="Numeric Udemy course ID (e.g. 6100015)")
    parser.add_argument(
        "--sections",
        type=str,
        help="Comma-separated section indices (e.g. 1,3,5) or 'all'",
    )
    parser.add_argument(
        "-w", "--width", type=int, default=80, help="Text wrap width (default: 80)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Delay between requests in seconds (default: 0.2)",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=project_root / "cookies.json",
        help="Path to exported Udemy session cookies (JSON). Default: cookies.json",
    )
    args = parser.parse_args()

    if not args.cookies.exists():
        print(f"Error: cookie file not found: {args.cookies}", file=sys.stderr)
        sys.exit(1)

    cookies = load_cookies(args.cookies)
    client = create_client(cookies)

    print("Checking session...")
    if not test_auth(client):
        print("Error: Session invalid. Check your cookie file.", file=sys.stderr)
        sys.exit(1)
    print("Session OK.\n")

    # Course ID
    course_id = args.course_id
    if not course_id:
        last_id = get_last_course_id(project_root)
        if last_id:
            prompt = f"Enter Udemy course ID [{last_id}]: "
            val = input(prompt).strip()
            course_id = val if val else last_id
        else:
            course_id = input("Enter Udemy course ID: ").strip()
    if not course_id:
        print("Error: No course ID provided.", file=sys.stderr)
        sys.exit(1)

    # Curriculum
    print(f"Fetching curriculum for course {course_id}...")
    try:
        items = fetch_curriculum(client, course_id)
    except httpx.HTTPStatusError as exc:
        print(f"Error fetching curriculum: {exc}", file=sys.stderr)
        sys.exit(1)

    sections = group_by_section(items)
    if not sections:
        print("No sections found.", file=sys.stderr)
        sys.exit(1)

    save_last_course_id(project_root, course_id)

    print(f"\nFound {len(sections)} section(s):")
    for i, (title, lectures) in enumerate(sections, start=1):
        print(f"  {i}. {title}  ({len(lectures)} lecture(s))")

    # Section selection
    selection = args.sections
    if not selection:
        selection = input("\nSelect sections (indices or 'all'): ").strip()
    if not selection or selection.lower() == "all":
        selected_indices = list(range(len(sections)))
    else:
        try:
            selected_indices = [int(x.strip()) - 1 for x in selection.split(",")]
        except ValueError:
            print("Error: Invalid selection.", file=sys.stderr)
            sys.exit(1)

    for idx in selected_indices:
        if idx < 0 or idx >= len(sections):
            print(f"Error: Index {idx + 1} out of range.", file=sys.stderr)
            sys.exit(1)

    # Directories
    vtt_cache_dir = project_root / ".tmp" / "vtt_cache"
    outputs_dir = project_root / "outputs"
    vtt_cache_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Write metadata for downstream tools (e.g., generate_notes.py)
    write_metadata(sections, course_id, outputs_dir)

    print(f"\nDownloading {sum(len(sections[i][1]) for i in selected_indices)} lecture(s)...\n")
    downloaded = 0
    skipped = 0

    for idx in selected_indices:
        title, lectures = sections[idx]
        print(f"Section: {title}")
        for l_idx, lecture in enumerate(lectures, start=1):
            ok, reason = process_lecture(
                client,
                course_id,
                lecture,
                idx + 1,
                l_idx,
                vtt_cache_dir,
                outputs_dir,
                args.width,
                args.delay,
            )
            if ok:
                downloaded += 1
                print(f"  [{l_idx}/{len(lectures)}] OK   – {lecture.get('title', 'Untitled')}")
            else:
                skipped += 1
                print(f"  [{l_idx}/{len(lectures)}] SKIP – {lecture.get('title', 'Untitled')} ({reason})")

    total = downloaded + skipped
    print(f"\n{'='*60}")
    print(f"Summary: {downloaded}/{total} downloaded, {skipped} skipped")
    print(f"VTT cache:  {vtt_cache_dir}")
    print(f"Outputs:    {outputs_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
