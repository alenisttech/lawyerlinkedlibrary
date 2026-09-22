import fitz
import sys

pdf = sys.argv[1]
doc = fitz.open(pdf)
out = []
for i, page in enumerate(doc):
    out.append(f"\n===== PAGE {i+1} =====\n")
    d = page.get_text("dict")
    for block in d["blocks"]:
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            txt = "".join(s["text"] for s in spans)
            size = max(s["size"] for s in spans)
            out.append(f"[{size:5.1f}] {txt}")
doc.close()

with open("academy_dump.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print(f"Wrote academy_dump.txt ({len(out)} lines)")