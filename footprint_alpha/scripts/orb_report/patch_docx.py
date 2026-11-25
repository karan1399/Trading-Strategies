#!/usr/bin/env python3
"""Post-process the audit docx: strip empty pgNumType, patch footer PAGE field
format, and remove the stray cached TOC-entry run that the placeholder script's
fldChar split leaves between the separate marker and the first placeholder."""
import os
import re
import shutil
import sys
import zipfile

P = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/z/my-project/download/ORB_Quantitative_Audit_and_Evolved_Design.docx"
TMP = "/tmp/docx_patch"

shutil.rmtree(TMP, ignore_errors=True)
with zipfile.ZipFile(P) as z:
    z.extractall(TMP)

doc = os.path.join(TMP, "word", "document.xml")
xml = open(doc, encoding="utf-8").read()
xml = xml.replace("<w:pgNumType/>", "")

# stray cached TOC entry: a run whose whole <w:t> is the first heading text,
# located between the TOC separate fldChar and the injected placeholder list.
def strip_stray(xml, heading):
    pat = re.compile(
        r"<w:r>(?:(?!</w:r>).)*?<w:t[^>]*>" + re.escape(heading) +
        r"</w:t>(?:(?!</w:r>).)*?</w:r>", re.S)
    m = pat.search(xml)
    if not m:
        return xml, 0
    run = m.group(0)
    if "PAGEREF" in run or "HYPERLINK" in run or "instrText" in run:
        return xml, 0
    return xml[:m.start()] + xml[m.end():], 1

n_stray = 0
for h in ["1. Executive Summary and Verdict"]:
    xml, k = strip_stray(xml, h)
    n_stray += k
open(doc, "w", encoding="utf-8").write(xml)

n_foot = 0
for f in os.listdir(os.path.join(TMP, "word")):
    if f.startswith("footer") and f.endswith(".xml"):
        fp = os.path.join(TMP, "word", f)
        s = open(fp, encoding="utf-8").read()
        s2, cnt = re.subn(r"(<w:instrText[^>]*>)\s*PAGE\s*(</w:instrText>)",
                          r"\1 PAGE \\* arabic \\* MERGEFORMAT \2", s)
        if cnt:
            open(fp, "w", encoding="utf-8").write(s2)
            n_foot += cnt

with zipfile.ZipFile(P, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(TMP):
        for f in files:
            full = os.path.join(root, f)
            z.write(full, os.path.relpath(full, TMP))
print(f"patched: stray_toc_runs={n_stray} footer_fields={n_foot}")
