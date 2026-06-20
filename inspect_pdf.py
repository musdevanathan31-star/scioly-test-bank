import sys, fitz
sys.stdout.reconfigure(encoding="utf-8")

pdf = sys.argv[1]
doc = fitz.open(pdf)
for pno, page in enumerate(doc, 1):
    print(f"\n========== PAGE {pno} ==========")
    text = page.get_text("text")
    print(text[:2500])
    print(f"... ({len(text)} chars total)")
