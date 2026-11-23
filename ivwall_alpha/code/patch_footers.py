#!/usr/bin/env python3
"""Patch docx footers for WPS: explicit PAGE format switch + strip empty pgNumType."""
import re
import shutil
import sys
import zipfile

path = sys.argv[1]
tmp = path + ".tmp"

with zipfile.ZipFile(path, "r") as zin:
    items = zin.infolist()
    data = {i.filename: zin.read(i.filename) for i in items}

for name in list(data.keys()):
    if re.match(r"word/footer\d*\.xml$", name):
        xml = data[name].decode("utf-8")
        xml = re.sub(
            r"(<w:instrText[^>]*>)\s*PAGE\s*(</w:instrText>)",
            r"\1 PAGE \\* arabic \\* MERGEFORMAT \2",
            xml,
        )
        data[name] = xml.encode("utf-8")
    if name == "word/document.xml":
        xml = data[name].decode("utf-8")
        xml = xml.replace("<w:pgNumType/>", "")
        data[name] = xml.encode("utf-8")

with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for i in items:
        zout.writestr(i, data[i.filename])
shutil.move(tmp, path)
print("footers patched:", path)
