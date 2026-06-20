import sys, fitz
sys.stdout.reconfigure(encoding="utf-8")
doc = fitz.open(sys.argv[1])
print(f"Total pages: {doc.page_count}")
# show last 3 pages
for pno in range(max(0, doc.page_count - 3), doc.page_count):
    print(f"\n========== PAGE {pno+1} ==========")
    print(doc[pno].get_text("text"))
