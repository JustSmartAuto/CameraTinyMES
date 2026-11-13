"""临时排查：工控机落盘报告 xlsx 结构（验证后删除）。"""
import io
import sys
import zipfile
from pathlib import Path

p = Path(r"d:\JustSmart\CameraTinyMES\reports\视觉改善分析报告_20261111091938.xlsx")
print("size:", p.stat().st_size, "mtime:", p.stat().st_mtime)
data = p.read_bytes()
print("head:", data[:4])
with zipfile.ZipFile(io.BytesIO(data)) as z:
    names = z.namelist()
    print("entries:", len(names))
    print("media:", [n for n in names if "media" in n][:5])
    print("cellimages:", [n for n in names if "cellimage" in n])
    print("drawings:", [n for n in names if "drawing" in n][:5])
    for n in names:
        if n.startswith("xl/worksheets/") and n.endswith(".xml"):
            xml = z.read(n).decode("utf-8", "ignore")
            print(n, "DISPIMG:", xml.count("DISPIMG"), "len:", len(xml))
    ct = z.read("[Content_Types].xml").decode("utf-8", "ignore")
    print("ct cellimages:", "cellimages" in ct)
    print("extra:", [n for n in names if n not in
          ("[Content_Types].xml", "_rels/.rels") and not
          n.startswith(("xl/worksheets", "xl/theme", "xl/styles", "xl/sharedStrings",
                        "xl/workbook", "xl/_rels", "docProps"))][:12])
