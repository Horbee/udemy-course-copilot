# Udemy Course Copilot

Batch-download readable course transcripts from Udemy via internal APIs, and generate AI-enhanced study notes from them.

## Overview

This project automates transcript extraction from Udemy courses and transforms them into rich, learnable Markdown files using OpenAI's API. It supports three workflows:

1. **Automatic API-based fetching** (recommended): Downloads VTT captions directly from Udemy's internal APIs and converts them to readable `.txt` files.
2. **Manual HTML parsing** (fallback): Extracts text from manually saved Udemy transcript HTML files.
3. **AI Study Notes Generation** (optional): Sends extracted transcripts to an LLM to produce comprehensive, Obsidian-compatible Markdown study guides.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) package manager
- Python >= 3.11
- An active Udemy subscription to the target course
- Browser cookie export capability (see [Exporting Cookies](#exporting-cookies))
- An [OpenAI API key](https://platform.openai.com/api-keys) (for Workflow C only)

## Quick Start

```bash
uv sync

# Run in interactive mode
# (Create a .env file first with your OpenAI key if you plan to use Workflow C)
uv run scripts/fetch_transcripts.py
```

## Workflow A: Automatic Transcript Fetching (Recommended)

The `fetch_transcripts.py` script uses your Udemy session cookies to call internal APIs, download lecture transcripts, and save them as readable text files.

### Step 1: Export your Udemy session cookies

You need to extract cookies from your browser after logging into Udemy.

**Recommended browser extensions:**

- **Chrome/Firefox**: "Get cookies.txt LOCALLY" or "Cookie-Editor"
- Export cookies for **www.udemy.com** in JSON format
- Save the file (e.g., `cookies.json`)

### Step 2: Run the fetcher

```bash
# Interactive mode (prompts for course ID and sections)
uv run scripts/fetch_transcripts.py --cookies cookies.json

# Or fully non-interactive
uv run scripts/fetch_transcripts.py \
  --cookies cookies.json \
  --course-id 6100015 \
  --sections all
```

**Features:**

- Auto-saves course ID to `.tmp/last_course_id.txt` for reuse on next run
- Prompt shows last used course ID as placeholder — just press Enter to reuse
- Downloads raw WebVTT files to `.tmp/vtt_cache/`
- Converts VTT to wrapped plain text in `outputs/`
- Writes `outputs/.metadata.json` containing full, untruncated lecture titles for downstream tools
- 200ms delay between requests to be polite to Udemy's servers

### CLI Reference for `fetch_transcripts.py`

| Flag               | Description                                                           |
| ------------------ | --------------------------------------------------------------------- |
| `--cookies PATH`   | Path to exported Udemy session cookies JSON (default: `cookies.json`) |
| `--course-id ID`   | Numeric Udemy course ID (e.g. `6100015`)                              |
| `--sections 1,3,5` | Comma-separated section indices, or `all`                             |
| `-w, --width N`    | Text wrap width in characters (default: 80)                           |
| `--delay SEC`      | Delay between API requests (default: 0.2)                             |
| `--skip-auth-test` | Skip the initial session check and fetch the course directly          |

The downloader uses `curl-cffi` with Chrome impersonation for browser-compatible
connections. If a Cloudflare challenge still occurs, open Udemy in Chrome, complete
the challenge, and export fresh cookies. `--skip-auth-test` only skips the initial
check; it does not resolve blocked course requests.

## Workflow B: Manual HTML Extraction (Fallback)

If you already have transcript HTML files saved from Udemy (e.g., via browser "Save As"), use this script.

```bash
# Place HTML files in transcripts/ directory
# Run the extractor
uv run scripts/extract_transcript.py
```

The script scans `transcripts/*.html`, extracts text from the rendered transcript panel, and writes clean `.txt` files to `outputs/`.

### CLI Reference for `extract_transcript.py`

| Flag                | Description                                               |
| ------------------- | --------------------------------------------------------- |
| `--input-dir PATH`  | Directory containing HTML files (default: `transcripts/`) |
| `--output-dir PATH` | Output directory for `.txt` files (default: `outputs/`)   |
| `-w, --width N`     | Text wrap width in characters (default: 80)               |

## Workflow C: AI Study Notes Generation (Optional)

Once you have extracted transcripts in `outputs/`, you can generate rich, Obsidian-compatible Markdown study notes using OpenAI's API.

> **💡 Full lecture titles:** `fetch_transcripts.py` writes `outputs/.metadata.json` containing the full, untruncated title for every lecture. `generate_notes.py` reads this automatically so your study guides show complete titles instead of the clipped filenames. If `.metadata.json` is missing (e.g., you used the manual HTML fallback), titles will gracefully fall back to the filename-based version.

```bash
# 1. Create a .env file with your OpenAI API key
echo "OPENAI_API_KEY=sk-..." > .env

# 2. Preview which files will be matched (dry run)
uv run scripts/generate_notes.py --pattern day_1 --dry-run

# Match multiple groups with regex
uv run scripts/generate_notes.py --pattern 'day_2|day_3' --dry-run

# 3. Generate the notes
uv run scripts/generate_notes.py --pattern day_1

# 4. Use a different model (e.g., for faster/cheaper generation)
uv run scripts/generate_notes.py --pattern day_1 --model gpt-4o-mini

# 5. Merge all matched transcripts into one synthesized note
uv run scripts/generate_notes.py --pattern day_1 --merge
```

**What you get:**

- One comprehensive Markdown file per pattern (e.g. `notes/day_1.md`)
- A `Sources` section near the top listing every matched transcript
- **Summary** of each lecture, or of the merged transcript set when using `--merge`
- **Key Concepts** with Obsidian callouts
- **Code Snippets** (formatted with language tags)
- **Extended Knowledge** — going beyond the lecture
- **Interview Q&A** — realistic technical interview questions
- **Flashcards** — spaced-repetition friendly Q&A pairs
- YAML frontmatter for Obsidian compatibility (tags, Dataview)

### CLI Reference for `generate_notes.py`

| Flag                | Description                                                                           |
| ------------------- | ------------------------------------------------------------------------------------- |
| `--pattern REGEX`   | _(Required)_ Regex to match `outputs/*.txt` filenames (e.g. `day_1`, `day_2\|day_3`) |
| `--model MODEL`     | OpenAI model to use (default: `gpt-4o`)                                               |
| `--output-dir PATH` | Output directory for `.md` files (default: `notes/`)                                  |
| `--api-key KEY`     | OpenAI API key override (default: reads from `.env` → `OPENAI_API_KEY`)               |
| `--merge`           | Send all matched transcripts to OpenAI as one transcript and generate one note         |
| `--dry-run`         | Show matched files without calling the API                                            |

**Obsidian Tips:**

- Open the `notes/` folder as an Obsidian vault (or a sub-folder within one).
- Tags in the frontmatter (`#llm-course`, `#day-1`) automatically appear in the tag pane.
- Callouts (`> [!IMPORTANT]`, `> [!TIP]`) render natively in Obsidian with distinct colors.
- Use Dataview queries to filter notes by tag, date, or model.

## Workflow D: Single-Lecture Note + Mind Map (Optional)

`obsidian_note.py` turns **one** transcript (and, optionally, its slide deck PDF) into a single
Obsidian-flavoured Markdown note, with a **foldable Mermaid mind map** at the top that you can
toggle open/closed inside Obsidian.

When `--slides` is given, the PDF is uploaded to OpenAI via the Files API and attached directly
to the request — the model reads the slide deck's native layout, text, diagrams, and images
itself, rather than us extracting text locally. This requires a **vision-capable model** (the
default, `gpt-4o`, already qualifies) and is subject to OpenAI's **32 MB per-file** limit. The
uploaded file is deleted again after the note is generated.

```bash
# Transcript only
uv run scripts/obsidian_note.py --transcript outputs/01_001_52932165_intro.txt

# Transcript + slide deck (the PDF itself is attached to the request)
uv run scripts/obsidian_note.py \
  --transcript outputs/01_001_52932165_intro.txt \
  --slides slides/01_intro.pdf

# Preview without calling the API
uv run scripts/obsidian_note.py --transcript outputs/01_001_52932165_intro.txt --dry-run

# Skip the mind map, or render it expanded by default
uv run scripts/obsidian_note.py --transcript outputs/01_001_52932165_intro.txt --no-mindmap
uv run scripts/obsidian_note.py --transcript outputs/01_001_52932165_intro.txt --expand-mindmap
```

**What you get:**

- One Markdown file per lecture (`notes/<transcript-stem>.md`)
- A collapsible `> [!example]- 🧠 Mind Map` callout containing a Mermaid `mindmap` diagram —
  click it in Obsidian to expand/collapse
- Summary, topic sections, key terms, and self-test prompts, same conventions as Workflow C
- YAML frontmatter recording the source transcript/slides and model used

### CLI Reference for `obsidian_note.py`

| Flag                 | Description                                                              |
| --------------------- | ------------------------------------------------------------------------ |
| `--transcript PATH`  | _(Required)_ Path to the lecture transcript `.txt` file                 |
| `--slides PATH`      | Path to the lecture's slide deck `.pdf` file (optional)                 |
| `--model MODEL`      | OpenAI model to use (default: `gpt-4o`)                                 |
| `--output-dir PATH`  | Output directory for the generated `.md` file (default: `notes/`)       |
| `--api-key KEY`      | OpenAI API key override (default: reads from `.env` → `OPENAI_API_KEY`) |
| `--no-mindmap`       | Skip generating the mind map section                                    |
| `--expand-mindmap`   | Render the mind map callout expanded by default (still foldable)        |
| `--dry-run`          | Show what would be processed without calling the API                    |

## Project Layout

```
udemy-course-copilot/
├── scripts/
│   ├── fetch_transcripts.py      # API-based automatic downloader + metadata writer
│   ├── extract_transcript.py     # HTML parser fallback
│   └── generate_notes.py         # AI-powered study guide generator
├── outputs/                       # Readable .txt transcripts
│   └── .metadata.json             # Full lecture titles (auto-generated by fetcher)
├── transcripts/                   # Manually saved HTML files (fallback)
├── notes/                         # Generated Obsidian-compatible Markdown
├── .tmp/
│   ├── vtt_cache/                 # Raw WebVTT subtitle files
│   └── last_course_id.txt         # Persists last used course ID
├── .env                           # Your OpenAI API key (ignored by git)
├── cookies.json                   # Your Udemy session cookies (ignored by git)
└── www.udemy.com.har             # (Optional) Network trace for debugging
```

## How It Works

### Automatic Fetching (`fetch_transcripts.py`)

1. **Auth**: Loads your browser cookies with their domain/path scope via `curl-cffi` using Chrome impersonation and validates the session against `/api-2.0/contexts/me/` (unless `--skip-auth-test` is set)
2. **Curriculum**: Fetches the full course structure from `/api-2.0/courses/{id}/subscriber-curriculum-items/`
3. **Lecture details**: For each selected lecture, calls `/api-2.0/users/me/subscribed-courses/{id}/lectures/{lecture_id}/` to get caption metadata
4. **VTT download**: Downloads the English caption's signed `.vtt` URL
5. **Parsing**: Strips WebVTT timestamps and tags, wraps text to 80 characters

### Manual Fallback (`extract_transcript.py`)

1. Parses HTML with BeautifulSoup
2. Targets `<span data-purpose="cue-text">` inside the transcript panel
3. Normalises whitespace and wraps text for readability

### AI Notes Generation (`generate_notes.py`)

1. **Discovery**: Matches `outputs/*.txt` files by a user-supplied pattern (e.g. `day_1`)
2. **Metadata lookup**: Loads `outputs/.metadata.json` to resolve full, untruncated lecture titles
3. **Enrichment**: Sends each transcript to the OpenAI API (one call per lecture) with a structured system prompt requesting Summary, Key Concepts, Code Snippets, Extended Knowledge, Interview Q&A, and Flashcards
4. **Retry logic**: Exponential backoff on transient OpenAI errors (429, 500, timeout)
5. **Compilation**: Assembles all enriched notes into one Markdown file with YAML frontmatter, Table of Contents, and Obsidian callouts

## Transcript Naming Convention

Files are prefixed with section and lecture indices plus the lecture title:

```
outputs/01_001_52932165_day_1_-_running_your_first_llm_locally_w.txt
outputs/02_015_52933073_day_2_-_open-source_llms_llama_mistral_d.txt
```

Where:

- `01` = section index
- `001` = lecture index within that section
- `52932165` = Udemy lecture ID

## Future Improvements

- [ ] **Multi-language caption support**: Allow selection of `en_US`, `de_DE`, `th_TH`, etc. Currently defaults to English.
- [ ] **Course ID auto-discovery**: Parse the numeric course ID from a Udemy URL slug (e.g. `course/{slug}` → `6100015`), removing the need to manually find the ID.
- [ ] **Parallel downloads**: Concurrent fetching of VTT files and lecture metadata via `asyncio` + `httpx.AsyncClient`.
- [ ] **Retry logic with exponential backoff**: Handle transient HTTP 429 / expired signed URL failures gracefully.
- [ ] **Curriculum caching**: Save course structure locally to avoid re-fetching when re-running for different sections.
- [ ] **Section selection persistence**: Remember last selected sections alongside the course ID.
- [ ] **Docker support**: Containerise the scripts for users without `uv` installed.
- [ ] **TUI / GUI**: Interactive terminal or web interface for browsing sections before downloading.
- [ ] **VTT-to-SRT export**: Option to save raw captions in SRT format alongside text.
- [ ] **Progress bars**: Add `tqdm` or `rich` progress indication for long batch downloads.

## License

[MIT](LICENSE) — Use at your own risk. This tool is for personal educational use. Respect Udemy's Terms of Service.
