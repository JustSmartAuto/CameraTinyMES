"""临时排查：对比库内 xlsx 与落盘 xlsx 的 DISPIMG 结构（验证后删除）。"""
import io
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, "src")
from cameratinymes.config import default_data_dir
from cameratinymes.storage import open_storage


def probe(label, data):
    print(f"===== {label} ({len(data)} B) =====")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        media = [n for n in names if n.startswith("xl/media/")]
        print("media:", len(media), media[:3])
        xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
        print("sheet1 DISPIMG:", xml.count("DISPIMG"))
        print("  <f> cells:", len(re.findall(r"<f[ >]", xml)))
        print("  DISPIMG in <f>:", len(re.findall(r"<f[^>]*>[^<]*DISPIMG", xml)))
        print("  DISPIMG in <v>/inline:", xml.count("DISPIMG")
              - len(re.findall(r"<f[^>]*>[^<]*DISPIMG", xml)))
        m = re.search(r"<c r=\"[A-Z]+\d+\"[^>]*>((?:(?!</c>).)*DISPIMG(?:(?!</c>).)*)</c>", xml)
        print("  sample cell:", m.group(0)[:220] if m else "-")
        ss = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore") if "xl/sharedStrings.xml" in names else ""
        print("sharedStrings DISPIMG:", ss.count("DISPIMG"))
        if "xl/cellimages.xml" in names:
            ci = z.read("xl/cellimages.xml").decode("utf-8", "ignore")
            print("cellimages entries:", ci.count("<xdr:pic") if "xdr:pic" in ci else ci.count("pic"))


p = Path(r"d:\JustSmart\CameraTinyMES\reports\视觉改善分析报告_20261111091938.xlsx")
probe("落盘文件(工控机)", p.read_bytes())

s = open_storage(default_data_dir())
row = s._fetchall(
    "SELECT data FROM report_files WHERE kind='xlsx'"
    " AND report_id=(SELECT MAX(report_id) FROM report_files WHERE kind='xlsx')"
    " ORDER BY part_no LIMIT 1")
s.close()
probe("库内存档", row[0][0])
