import os
import re
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from dotenv import load_dotenv

from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SERVICE_ROLE_KEY:
    raise ValueError("Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env file")

BUCKET_NAME = "acts-pdfs"
STORAGE_FOLDER = "library"

#PDF_FOLDER = r"C:\Users\Lenovo\OneDrive\Pictures\Desktop\Internship\Lawyerlinked_library\lawyerlinkedlibrary\library_data"

MANIFEST_LOCAL_PATH = "manifest.json"   # will be saved in the current folder
MANIFEST_STORAGE_PATH = f"{STORAGE_FOLDER}/manifest.json"

supabase = create_client(SUPABASE_URL, SERVICE_ROLE_KEY)


# ---------------------------------------------------------------------------
# Text normalization (defined BEFORE extract_lines so editors don't warn)
# ---------------------------------------------------------------------------

EM_DASH = "\u2014"
EN_DASH = "\u2013"
HORIZONTAL_BAR = "\u2015"
MINUS_SIGN = "\u2212"
BULLET = "\u2022"

# All separator characters used between a section title and its body.
BODY_SEPARATORS = (EM_DASH, EN_DASH, HORIZONTAL_BAR, MINUS_SIGN, BULLET)

# Broken byte sequences -> correct characters. Keys/values are escapes so this
# file stays pure ASCII and cannot be corrupted by copy-paste.
_MOJIBAKE = {
    # Em dash variants
    "\u00e2\u20ac\u201d": EM_DASH,       # a-euro-rightdoublequote -> em dash
    "\u00e2\u0080\u0094": EM_DASH,       # a-control80-control94   -> em dash
    # En dash variants
    "\u00e2\u20ac\u201c": EN_DASH,       # a-euro-leftdoublequote  -> en dash
    "\u00e2\u0080\u0093": EN_DASH,       # a-control80-control93   -> en dash
    # Quotes
    "\u00e2\u20ac\u2122": "\u2019",      # a-euro-trademark        -> right single quote
    "\u00e2\u20ac\u02dc": "\u2018",      # a-euro-smalltilde       -> left single quote
    "\u00e2\u20ac\u0153": "\u201c",      # a-euro-oeligature       -> left double quote
    "\u00e2\u20ac\u009d": "\u201d",      # a-euro-control-9d       -> right double quote
    "\u00e2\u20ac\u0022": "\u201d",      # a-euro-straightquote    -> right double quote
    # Bullet variants (both byte patterns seen in Indian bare Act PDFs)
    "\u00e2\u20ac\u00a2": BULLET,        # a-euro-cent             -> bullet
    "\u00e2\u20ac\u2022": BULLET,        # a-euro-bullet           -> bullet
    # Stray artifacts
    "\u00c2": "",                        # A-circumflex            -> (nothing)
}


def fix_mojibake(text: str) -> str:
    for bad, good in _MOJIBAKE.items():
        text = text.replace(bad, good)
    return text


def strip_amendment_brackets(text: str) -> str:
    """
    Indian bare Acts wrap provisions that were substituted or inserted by a
    later amendment in square brackets, e.g.:
        [3. Central Pollution Control Board.--The Central Pollution ...]
    The brackets are purely typographic (a drafting convention showing what
    was substituted/inserted); the text inside is the real, current section.
    Left in place, a bracketed section number like "[3." never matches
    SECTION_START_RE (which requires the line to *start* with a digit), so
    the whole bracketed section -- title and body -- gets swallowed into
    whatever structural element precedes it (usually the chapter title) by
    the "keep consuming lines until a real section start" loops. Stripping
    the brackets before any structural parsing makes bracketed sections
    parse exactly like ordinary ones.
    """
    return text.replace('[', '').replace(']', '')


def clean_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def _split_on_separator(text: str):
    """
    Split text at the first body separator.
    Returns (title_part, body_part) or (text, None) if no separator found.
    """
    earliest = None
    for sep in BODY_SEPARATORS:
        i = text.find(sep)
        if i != -1 and (earliest is None or i < earliest):
            earliest = i
    if earliest is None:
        return text, None
    return text[:earliest], text[earliest + 1:]


# ---------------------------------------------------------------------------
# PDF extraction via pdftotext -layout
# ---------------------------------------------------------------------------

def extract_lines(pdf_path: str):
    """
    Extract lines via pdftotext -layout. We lose font sizes but gain intact
    roman numerals in CHAPTER headings, sections not split at visual line
    breaks, and correct reading order.
    Returns list of (text, size) where size is always 0.0 (no size info).
    """
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", pdf_path, tmp_path],
            check=True, capture_output=True,
        )
        with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    lines = []
    for line in raw.splitlines():
        txt = fix_mojibake(line.rstrip())
        txt = strip_amendment_brackets(txt)
        if not txt.strip():
            continue
        lines.append((txt.strip(), 0.0))
    return lines


# ---------------------------------------------------------------------------
# Page-artifact cleanup
# ---------------------------------------------------------------------------

PAGE_NUM_RE = re.compile(r'^\s*\d{1,4}\s*$')
PAGE_NUM_MARKER_RE = re.compile(r'^\s*[*\u2020\u2021]?\s*\d{1,4}\s*$')

FOOTNOTE_PREFIX_RE = re.compile(
    r'^\s*(?:[*\u2020\u2021]|'
    r'\d{1,2}\.\s+(?:Omitted|Inserted|Substituted|Added|Renumbered|Repealed|See|Vide|'
    r'The\s+provisions\s+of\s+the\s+Act\s+have\s+been\s+brought\s+into\s+force)\b|'
    r'(?:Vide|See)\s+notification|'
    r'S\.?O\.?\s*\d+|'
    r'G\.?S\.?R\.?\s*\d+)',
    re.IGNORECASE
)
GAZETTE_INLINE_RE = re.compile(
    r'\b(?:vide notification|Gazette of India|see Gazette|'
    r'notification No\.|S\.?O\.?\s*\d+\s*\(E\)|G\.?S\.?R\.?\s*\d+)\b',
    re.IGNORECASE
)

# Section start pattern: Arabic (1, 1A) or Roman (I, II, XIV) at line start.
SECTION_START_RE = re.compile(r'^(?:\d+[A-Z]?|[IVXLC]+)\.\s')


def drop_running_headers(lines, min_repeats=2):
    """
    Drop lines that repeat across pages (running headers/footers). Never
    drop structural lines: chapter headings, section starts, THE SCHEDULE,
    or the title lines that immediately follow a CHAPTER heading.
    """
    from collections import Counter
    counts = Counter(txt for txt, _ in lines)

    chapter_title_lines = set()
    for i, (txt, _size) in enumerate(lines):
        if _is_real_chapter_heading(txt):
            for j in range(i + 1, min(i + 3, len(lines))):
                nxt = lines[j][0]
                if SECTION_START_RE.match(nxt):
                    break
                if _is_real_chapter_heading(nxt):
                    break
                chapter_title_lines.add(nxt)

    def is_structural(txt):
        if _is_real_chapter_heading(txt):
            return True
        if SECTION_START_RE.match(txt):
            return True
        if re.match(r'^THE\s+SCHEDULE\b', txt, re.IGNORECASE):
            return True
        if txt in chapter_title_lines:
            return True
        return False

    return [
        (txt, size) for txt, size in lines
        if is_structural(txt)
        or counts[txt] < min_repeats
        or len(txt) > 120
    ]


def drop_page_artifacts(lines):
    out = []
    for txt, size in lines:
        if PAGE_NUM_RE.match(txt) or PAGE_NUM_MARKER_RE.match(txt):
            continue
        if FOOTNOTE_PREFIX_RE.match(txt):
            continue
        if GAZETTE_INLINE_RE.search(txt) and len(txt) < 400:
            continue
        out.append((txt, size))
    return out


def strip_trailing_page_numbers(lines):
    cleaned = []
    for txt, size in lines:
        new = re.sub(r'(?<=[.\)"\'])\s+\d{1,4}\s*$', '', txt).rstrip()
        cleaned.append((new if new else txt, size))
    return cleaned


# ---------------------------------------------------------------------------
# Structural parsing
# ---------------------------------------------------------------------------

# Captures the roman numeral in group(1) and everything after it in group(2),
# so callers can decide whether the "rest" of the line looks like a genuine
# heading (empty or ALL CAPS) or ordinary sentence-case prose (a footnote /
# proviso that merely happens to start with the word "Chapter").
CHAPTER_RE = re.compile(r'^CHAPTER\b\s*([IVXLC]*)\b(.*)$', re.IGNORECASE)

SECTION_LINE_RE = re.compile(r'^\s*(\d+[A-Z]?|[IVXLC]+)\.\s+(.+)$')

MAX_SECTION_TITLE = 200


def _is_real_chapter_heading(txt):
    """
    True only for genuine chapter headings: 'CHAPTER IV' alone, or
    'CHAPTER IV - TITLE' where TITLE is upper-case (as these bare Acts
    always print real chapter titles). Rejects footnote/proviso prose that
    merely starts with the words 'Chapter IV' followed by an ordinary
    sentence, e.g. 'Chapter IV are brought into force in the Union
    territory of Pondicherry, were entitled to practise...' -- that is a
    proviso, not a heading, and must not be treated as a chapter boundary.
    """
    m = CHAPTER_RE.match(txt)
    if not m:
        return False
    if not m.group(1):
        # bare "CHAPTER" with no roman numeral following -> not a real heading
        return False
    rest = m.group(2).strip().lstrip(''.join(BODY_SEPARATORS) + ' -:').strip()
    if not rest:
        return True
    letters = [c for c in rest if c.isalpha()]
    if not letters:
        return True
    return all(c.isupper() for c in letters)


def _roman_to_int(s):
    if not s:
        return None
    s = s.upper()
    vals = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100}
    total, prev = 0, 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total


def _num_int(num):
    m = re.match(r'^(\d+)', num or "")
    if m:
        return int(m.group(1))
    return _roman_to_int(num)


def extract_sections_from_block(block_lines):
    """
    Walk a block of lines, emit only sections that have a real body.
    TOC entries (no separator) are dropped. Wrapped titles and wrapped
    bodies are rejoined. Backward-numbered fragments are merged into the
    previous section. Handles Arabic and Roman section numbers, and all
    the separator characters used in Indian bare Acts.
    """
    sections = []
    current = None

    def flush():
        nonlocal current
        if current and current["has_body"]:
            title = clean_text(" ".join(current["title_parts"]))
            if len(title) <= MAX_SECTION_TITLE:
                sections.append({
                    "section_number": current["num"],
                    "section_title": title,
                    "text": clean_text(" ".join(current["body_parts"]))[:8000],
                    "_num_int": _num_int(current["num"]),
                })
        current = None

    for txt, _size in block_lines:
        if _is_real_chapter_heading(txt):
            continue
        m = SECTION_LINE_RE.match(txt) if SECTION_START_RE.match(txt) else None
        if m:
            num, rest = m.group(1), m.group(2)
            title_part, body_part = _split_on_separator(rest)
            flush()
            if body_part is not None:
                current = {
                    "num": num.strip(),
                    "title_parts": [title_part.rstrip(". ").strip()],
                    "body_parts": [body_part.strip()],
                    "has_body": True,
                }
            else:
                current = {
                    "num": num.strip(),
                    "title_parts": [rest.rstrip(". ").strip()],
                    "body_parts": [],
                    "has_body": False,
                }
            continue

        if current is None:
            continue

        if current["has_body"]:
            current["body_parts"].append(txt)
        else:
            title_tail, body_tail = _split_on_separator(txt)
            if body_tail is not None:
                if title_tail.strip():
                    current["title_parts"].append(title_tail.strip())
                current["body_parts"].append(body_tail.strip())
                current["has_body"] = True
            else:
                current["title_parts"].append(txt)

    flush()

    # Merge backward-numbered fragments (page-broken sections).
    merged = []
    for sec in sections:
        if (merged
                and sec["_num_int"] is not None
                and merged[-1]["_num_int"] is not None
                and sec["_num_int"] < merged[-1]["_num_int"]):
            prev = merged[-1]
            frag = " ".join([sec["section_title"], sec["text"]]).strip()
            prev["text"] = (prev["text"] + " " + frag).strip()[:8000]
        else:
            merged.append(sec)

    # Drop earlier of duplicate section numbers (footnote promoted to section
    # followed by the real section with the same number).
    deduped = []
    for i, sec in enumerate(merged):
        is_dupe_of_later = False
        for later in merged[i + 1:]:
            if later["section_number"] == sec["section_number"]:
                is_dupe_of_later = True
                break
        if is_dupe_of_later:
            continue
        deduped.append(sec)

    for sec in deduped:
        sec.pop("_num_int", None)
    return deduped


def split_chapters_by_content(lines):
    """
    Find CHAPTER headings, form candidate blocks, drop any that has no real
    section body (drops the TOC). Returns [(chapter_number_or_None, lines)].
    """
    chapter_starts = []
    for i, (txt, _size) in enumerate(lines):
        if _is_real_chapter_heading(txt):
            chapter_starts.append(i)

    if not chapter_starts:
        return [(None, lines)]

    candidates = []
    if chapter_starts[0] > 0:
        candidates.append((None, lines[:chapter_starts[0]]))
    for k, pos in enumerate(chapter_starts):
        end = chapter_starts[k + 1] if k + 1 < len(chapter_starts) else len(lines)
        m = CHAPTER_RE.match(lines[pos][0])  # safe: pos is already a confirmed real heading
        num = (m.group(1) or "").upper() or None
        candidates.append((num, lines[pos:end]))

    body_chapters = []
    for num, ch_lines in candidates:
        if extract_sections_from_block(ch_lines):
            body_chapters.append((num, ch_lines))
    return body_chapters


def parse_act(lines, full_name):
    year_match = re.search(r'\b(18|19|20)\d{2}\b', full_name)
    year = int(year_match.group(0)) if year_match else None

    short_name = re.sub(r',\s*\d{4}$', '', full_name).strip()
    short_name = re.sub(r'\s*\(\d+\)$', '', short_name).strip()

    act = {
        "short_name": short_name,
        "full_name": full_name,
        "year": year,
        "status": "current",
        "category": "Special",
        "description": "",
        "preamble": "",
        "chapters": [],
    }

    body_text = "\n".join(t for t, _ in lines)

    preamble_match = re.search(
        r'(An Act to[^\n]+(?:\n[^\n]+)*?)(?=\n\s*(?:BE it enacted|CHAPTER|PRELIMINARY|\d+\.\s))',
        body_text, re.IGNORECASE
    )
    if preamble_match:
        act["preamble"] = clean_text(preamble_match.group(1))[:3000]

    chapters_raw = split_chapters_by_content(lines)

    for chapter_num, ch_lines in chapters_raw:
        chapter_title = ""
        if chapter_num and ch_lines:
            first = ch_lines[0][0]
            chapter_title = re.sub(
                r'^CHAPTER\s*[IVXLC]*\s*[\u2014\u2015\-:.]?\s*',
                '', first, flags=re.IGNORECASE
            ).strip()
            extra = []
            for txt, _ in ch_lines[1:]:
                if SECTION_START_RE.match(txt):
                    break
                if _is_real_chapter_heading(txt):
                    break
                if txt.upper() == "SECTIONS":
                    continue
                extra.append(txt)
            if extra:
                chapter_title = clean_text(chapter_title + " " + " ".join(extra))

        sections = extract_sections_from_block(ch_lines)
        if not sections:
            continue

        act["chapters"].append({
            "chapter_number": chapter_num,
            "chapter_title": clean_text(chapter_title) if chapter_num else None,
            "sections": sections,
        })

    def sort_key(ch):
        n = ch.get("chapter_number")
        if not n:
            return (0, 0)
        return (1, _roman_to_int(n) or 999)

    act["chapters"].sort(key=sort_key)
    return act


# ---------------------------------------------------------------------------
# Manifest + main
# ---------------------------------------------------------------------------

def make_safe_filename(name):
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'\s+', '_', name.strip()).lower()
    return name[:90]


def write_and_upload_manifest(entries):
    merged = {}
    try:
        existing_bytes = supabase.storage.from_(BUCKET_NAME).download(MANIFEST_STORAGE_PATH)
        for old in json.loads(existing_bytes.decode("utf-8")).get("acts", []):
            merged[old["id"]] = old
        print(f"Loaded existing manifest: {len(merged)} acts")
    except Exception:
        print("No existing manifest found (first run) - creating a new one")
    for e in entries:
        merged[e["id"]] = e

    entries = sorted(merged.values(), key=lambda e: e["fullName"].lower())
    manifest = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "acts": entries,
    }
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2)

    with open(MANIFEST_LOCAL_PATH, "w", encoding="utf-8") as f:
        f.write(manifest_text)
    print(f"Saved local manifest: {MANIFEST_LOCAL_PATH}")

    supabase.storage.from_(BUCKET_NAME).upload(
        path=MANIFEST_STORAGE_PATH,
        file=manifest_text.encode("utf-8"),
        file_options={
            "content-type": "application/json",
            "upsert": "true",
            "cache-control": "60",
        },
    )
    manifest_url = supabase.storage.from_(BUCKET_NAME).get_public_url(MANIFEST_STORAGE_PATH).rstrip("?")
    print(f"Uploaded manifest ({len(entries)} acts): {manifest_url}")


def process_all_pdfs():
    print("Starting conversion of PDFs from Supabase Storage...\n")

    # 1. List all files in the library folder
    try:
        files = supabase.storage.from_(BUCKET_NAME).list(STORAGE_FOLDER)
    except Exception as e:
        print(f"Failed to list files: {e}")
        return

    # Keep only PDF files
    pdf_files = [f for f in files if f["name"].lower().endswith(".pdf")]
    print(f"Found {len(pdf_files)} PDF files in Storage\n")

    manifest_entries = []

    for file_info in pdf_files:
        filename = file_info["name"]
        storage_path = f"{STORAGE_FOLDER}/{filename}"

        print(f"Processing: {filename}")

        try:
            # 2. Download PDF from Storage to a temporary file
            pdf_bytes = supabase.storage.from_(BUCKET_NAME).download(storage_path)

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name

            # 3. Extract text from the temporary PDF
            lines = extract_lines(tmp_path)
            lines = drop_running_headers(lines, min_repeats=2)
            lines = drop_page_artifacts(lines)
            lines = strip_trailing_page_numbers(lines)
            print(f"  -> Extracted {len(lines)} lines")

            # Clean up temporary file
            os.unlink(tmp_path)

            full_name = filename.replace(".pdf", "").replace("_", " ").strip()
            full_name = re.sub(r'\s*\(\d+\)$', '', full_name).strip()

            act_json = parse_act(lines, full_name)
            total_sections = sum(len(ch["sections"]) for ch in act_json["chapters"])
            print(f"  -> {len(act_json['chapters'])} chapters, {total_sections} sections")

            if total_sections == 0:
                print("  Warning: 0 sections extracted. Skipping.")
                continue

            safe_name = make_safe_filename(full_name)
            json_filename = f"{safe_name}.json"
            json_storage_path = f"{STORAGE_FOLDER}/{json_filename}"

            # 4. Upload the JSON
            json_bytes = json.dumps(act_json, ensure_ascii=False, indent=2).encode("utf-8")
            supabase.storage.from_(BUCKET_NAME).upload(
                path=json_storage_path,
                file=json_bytes,
                file_options={"content-type": "application/json", "upsert": "true"}
            )

            public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(json_storage_path).rstrip("?")
            
            # Also get PDF public URL
            pdf_public_url = supabase.storage.from_(BUCKET_NAME).get_public_url(storage_path).rstrip("?")

            manifest_entries.append({
                "id": safe_name,
                "shortName": act_json["short_name"],
                "fullName": act_json["full_name"],
                "year": str(act_json["year"]) if act_json["year"] else "",
                "status": act_json["status"],
                "category": act_json["category"],
                "description": act_json["description"],
                "url": public_url,          # JSON URL
                "pdfUrl": pdf_public_url,   # PDF URL
            })

            print(f"  -> Uploaded JSON: {json_storage_path}")
            print("  Done\n")

        except Exception as e:
            print(f"  Failed: {e}\n")

    if manifest_entries:
        try:
            write_and_upload_manifest(manifest_entries)
        except Exception as e:
            print(f"Manifest failed: {e}")

    print("\nAll PDFs processed from Storage!")

if __name__ == "__main__":
    process_all_pdfs()