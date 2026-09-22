import subprocess
import tempfile
import os

PDFTOTEXT = "pdftotext"  # resolves via PATH


def extract_lines(pdf_path: str):
    """
    Extract lines via pdftotext -layout, in reading order. We lose font sizes
    but gain: intact roman numerals in CHAPTER headings, sections that aren't
    split at visual line breaks, and cleaner ordering.

    Returns list of (text, size) where size is always 0.0 (no size info from
    pdftotext). Downstream code that used size for filtering now uses other
    cues.
    """
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            [PDFTOTEXT, "-layout", "-enc", "UTF-8", pdf_path, tmp_path],
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
        if not txt.strip():
            continue
        # pdftotext returns leading whitespace that encodes column position;
        # strip it (we don't use it) but keep the text.
        lines.append((txt.strip(), 0.0))
    return lines