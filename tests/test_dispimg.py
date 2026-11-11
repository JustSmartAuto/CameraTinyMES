"""图片嵌入单元格（WPS `DISPIMG`）测试。

覆盖 `wpsdispimg.inject_dispimg` 的产物结构与 `report.layout_to_xlsx` 的
「嵌入单元格模式」组合流程，全部在内存中完成，不写文件、不连数据库。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from PIL import Image

from cameratinymes.report import CellSpec, ColumnSpec, ImageSpec, LayoutModel, layout_to_xlsx
from cameratinymes.wpsdispimg import (
    CELLIMAGES_CONTENT_TYPE,
    CELLIMAGES_PART,
    CELLIMAGES_RELS,
    CELLIMAGES_REL_TYPE,
    NS_A,
    NS_DOC_REL,
    NS_XDR,
    DispImage,
    inject_dispimg,
)

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _make_image(size: tuple[int, int] = (64, 48),
                color: tuple[int, int, int] = (200, 30, 30),
                fmt: str = "JPEG") -> bytes:
    """生成真实可解码的小图。

    cellimages 的 `a:ext` 取自图片真实像素尺寸（`a:ext` 为 0 时 WPS 不绘制，
    单元格显示空白），因此不能用占位字节。
    """
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


JPEG = _make_image()
JPEG2 = _make_image(color=(30, 120, 220))
PNG = _make_image(fmt="PNG", color=(30, 200, 120))


# ---------- 构造与解析辅助 ----------

def make_xlsx(sheets: dict[str, dict[str, object]] | None = None) -> bytes:
    """用 openpyxl 造一个工作簿；sheets = {表名: {单元格: 值}}。"""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, cells in (sheets or {"Sheet1": {"A1": "标题"}}).items():
        sheet = workbook.create_sheet(name)
        for ref, value in cells.items():
            sheet[ref] = value
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def make_styled_xlsx(ref: str = "C3") -> bytes:
    """造一个带样式的占位单元格，用于验证注入后样式保留。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["A1"] = "标题"
    cell = sheet[ref]
    cell.value = "占位"
    cell.font = Font(bold=True, size=16)
    cell.fill = PatternFill("solid", fgColor="FFFF0000")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def parts(xlsx_bytes: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def sheet_part(xlsx_bytes: bytes, sheet_name: str) -> str:
    """按工作表名解析出 zip 内 sheetN.xml 的路径。"""
    entries = parts(xlsx_bytes)
    workbook = ET.fromstring(entries["xl/workbook.xml"])
    rels = ET.fromstring(entries["xl/_rels/workbook.xml.rels"])
    rel_map = {rel.get("Id"): rel.get("Target") for rel in rels}
    for sheet in workbook.iter(f"{{{NS_MAIN}}}sheet"):
        if sheet.get("name") == sheet_name:
            target = rel_map[sheet.get(f"{{{NS_DOC_REL}}}id")]
            return target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    raise AssertionError(f"工作簿里找不到工作表 {sheet_name}")


def cellimages(xlsx_bytes: bytes) -> dict[str, str]:
    """解析 cellimages.xml：图片 ID → 媒体文件相对路径。"""
    entries = parts(xlsx_bytes)
    root = ET.fromstring(entries[CELLIMAGES_PART])
    rels = ET.fromstring(entries[CELLIMAGES_RELS])
    rel_map = {rel.get("Id"): rel.get("Target") for rel in rels}
    found: dict[str, str] = {}
    for picture in root.iter(f"{{{NS_XDR}}}pic"):
        name = picture.find(
            f"{{{NS_XDR}}}nvPicPr/{{{NS_XDR}}}cNvPr").get("name")
        blip = picture.find(f"{{{NS_XDR}}}blipFill/{{{NS_A}}}blip")
        found[name] = rel_map[blip.get(f"{{{NS_DOC_REL}}}embed")]
    return found


def cell_value(xlsx_bytes: bytes, sheet_name: str = "Sheet1", ref: str = "C3"):
    workbook = load_workbook(io.BytesIO(xlsx_bytes))
    try:
        return workbook[sheet_name][ref].value
    finally:
        workbook.close()


def formula_of(xlsx_bytes: bytes, sheet_name: str = "Sheet1", ref: str = "C3") -> str:
    value = cell_value(xlsx_bytes, sheet_name, ref)
    assert value is not None, f"{sheet_name}!{ref} 没有内容"
    return str(value).lstrip("=")


def dispimg_id(formula: str) -> str:
    """从 `_xlfn.DISPIMG("ID_xxx",1)` 里取出图片 ID。"""
    start = formula.index('DISPIMG("') + len('DISPIMG("')
    return formula[start:formula.index('"', start)]


# ---------- inject_dispimg ----------

class InjectDispImageTest(unittest.TestCase):
    def test_single_jpeg_writes_formula_and_parts(self) -> None:
        source = make_xlsx()
        result = inject_dispimg(source, [DispImage(cell="C3", data=JPEG)])

        formula = formula_of(result, ref="C3")
        image_id = dispimg_id(formula)
        self.assertTrue(image_id.startswith("ID_"))
        self.assertTrue(formula.endswith('",1)'))

        entries = parts(result)
        self.assertEqual(entries[f"xl/media/dispimg1.jpeg"], JPEG)
        self.assertIn(CELLIMAGES_PART, entries)
        self.assertIn(CELLIMAGES_RELS, entries)
        self.assertEqual(cellimages(result), {image_id: "media/dispimg1.jpeg"})

        # Content_Types：WPS 私有部件类型 + jpeg 扩展名
        content_types = entries["[Content_Types].xml"].decode("utf-8")
        self.assertIn(f'PartName="/{CELLIMAGES_PART}"', content_types)
        self.assertIn(f'ContentType="{CELLIMAGES_CONTENT_TYPE}"', content_types)
        self.assertIn('Extension="jpeg"', content_types)

        # workbook.xml.rels：必须是 WPS 私有关系类型，否则 WPS 打开显示 #REF!
        workbook_rels = entries["xl/_rels/workbook.xml.rels"].decode("utf-8")
        self.assertIn(CELLIMAGES_REL_TYPE, workbook_rels)
        self.assertIn('Target="cellimages.xml"', workbook_rels)

        # 单元格缓存值，与 WPS 生成的文件保持一致
        sheet_xml = entries[sheet_part(result, "Sheet1")].decode("utf-8")
        self.assertIn(f'<v>=DISPIMG("{image_id}",1)</v>', sheet_xml)

        # a:ext 必须是真实尺寸（64×48 px → 609600×457200 EMU）；
        # 写成 0 时 WPS 不绘制，单元格显示空白。
        cellimages_xml = entries[CELLIMAGES_PART].decode("utf-8")
        self.assertIn('<a:ext cx="609600" cy="457200"/>', cellimages_xml)
        self.assertIn('<a:picLocks noChangeAspect="1"/>', cellimages_xml)

    def test_multiple_images_unique_ids_and_extensions(self) -> None:
        source = make_xlsx()
        result = inject_dispimg(source, [
            DispImage(cell="C3", data=JPEG),
            DispImage(cell="D4", data=JPEG2),
            DispImage(cell="H4", data=PNG, extension="png"),
        ])

        ids = [dispimg_id(formula_of(result, ref=ref)) for ref in ("C3", "D4", "H4")]
        self.assertEqual(len(set(ids)), 3, "每张图应各自生成唯一 ID")
        self.assertEqual(sorted(cellimages(result)), sorted(ids))

        entries = parts(result)
        self.assertEqual(entries["xl/media/dispimg1.jpeg"], JPEG)
        self.assertEqual(entries["xl/media/dispimg2.jpeg"], JPEG2)
        self.assertEqual(entries["xl/media/dispimg3.png"], PNG)

        content_types = entries["[Content_Types].xml"].decode("utf-8")
        self.assertIn('Extension="jpeg"', content_types)
        self.assertIn('Extension="png"', content_types)
        self.assertEqual(content_types.count(f'PartName="/{CELLIMAGES_PART}"'), 1)

    def test_target_sheet_selected_by_name(self) -> None:
        source = make_xlsx({
            "汇总": {"B2": "汇总占位"},
            "明细": {"B2": "明细占位"},
        })
        result = inject_dispimg(source, [DispImage(cell="B2", data=JPEG)], sheet_name="明细")

        image_id = dispimg_id(formula_of(result, sheet_name="明细", ref="B2"))
        self.assertTrue(image_id.startswith("ID_"))
        self.assertEqual(cell_value(result, "汇总", "B2"), "汇总占位")

        entries = parts(result)
        self.assertIn(b"DISPIMG", entries[sheet_part(result, "明细")])
        self.assertNotIn(b"DISPIMG", entries[sheet_part(result, "汇总")])

    def test_empty_images_returns_original_bytes(self) -> None:
        source = make_xlsx()
        self.assertEqual(inject_dispimg(source, []), source)
        self.assertNotIn(CELLIMAGES_PART, parts(source))

    def test_missing_cell_is_created(self) -> None:
        source = make_xlsx({"Sheet1": {"A1": "只有一格"}})
        result = inject_dispimg(source, [DispImage(cell="G9", data=JPEG)])

        self.assertTrue(formula_of(result, ref="G9").startswith('_xlfn.DISPIMG("ID_'))
        sheet_xml = parts(result)[sheet_part(result, "Sheet1")].decode("utf-8")
        self.assertIn('<row r="9"', sheet_xml)
        self.assertIn('<c r="G9" t="str">', sheet_xml)

    def test_keeps_cell_style(self) -> None:
        result = inject_dispimg(make_styled_xlsx("C3"), [DispImage(cell="C3", data=JPEG)])

        workbook = load_workbook(io.BytesIO(result))
        try:
            cell = workbook["Sheet1"]["C3"]
            self.assertEqual(str(cell.value).lstrip("=")[:13], "_xlfn.DISPIMG")
            self.assertTrue(cell.font.bold, "注入后应保留原字体样式")
            self.assertIn("FFFF0000", str(cell.fill.fgColor.rgb))
        finally:
            workbook.close()

    def test_overwrites_existing_cell_value(self) -> None:
        source = make_xlsx({"Sheet1": {"C3": "原有文字"}})
        result = inject_dispimg(source, [DispImage(cell="C3", data=JPEG)])

        self.assertTrue(formula_of(result, ref="C3").startswith('_xlfn.DISPIMG("ID_'))
        self.assertEqual(len(cellimages(result)), 1)
        # 关系与 Content_Types 只登记一次
        entries = parts(result)
        workbook_rels = entries["xl/_rels/workbook.xml.rels"].decode("utf-8")
        self.assertEqual(workbook_rels.count(CELLIMAGES_REL_TYPE), 1)
        content_types = entries["[Content_Types].xml"].decode("utf-8")
        self.assertEqual(content_types.count(f'PartName="/{CELLIMAGES_PART}"'), 1)

    def test_explicit_image_id_is_used(self) -> None:
        result = inject_dispimg(
            make_xlsx(), [DispImage(cell="C3", data=JPEG, image_id="ID_FIXED0001")])
        self.assertEqual(dispimg_id(formula_of(result, ref="C3")), "ID_FIXED0001")
        self.assertIn("ID_FIXED0001", cellimages(result))

    def test_images_spread_over_several_sheets(self) -> None:
        """导出用多工作表：每张图按 DispImage.sheet 落到各自的表。"""
        source = make_xlsx({"A0": {"B2": "占位"}, "A0渲染图": {"B2": "占位"}})
        result = inject_dispimg(source, [
            DispImage(cell="B2", data=JPEG, sheet="A0"),
            DispImage(cell="B2", data=PNG, extension="png", sheet="A0渲染图"),
        ])

        id_a = dispimg_id(formula_of(result, "A0", "B2"))
        id_b = dispimg_id(formula_of(result, "A0渲染图", "B2"))
        self.assertNotEqual(id_a, id_b)
        self.assertEqual(cellimages(result)[id_a], "media/dispimg1.jpeg")
        self.assertEqual(cellimages(result)[id_b], "media/dispimg2.png")


# ---------- 与版面生成的组合流程 ----------

class LayoutDispImageTest(unittest.TestCase):
    def _layout(self) -> LayoutModel:
        return LayoutModel(
            sheet_name="改善报告",
            title="测试报告",
            columns=[ColumnSpec("A", 10), ColumnSpec("B", 20), ColumnSpec("C", 20)],
            cells=[
                CellSpec(row=1, col="A", text="标题"),
                CellSpec(row=3, col="B", text=""),
                CellSpec(row=3, col="C", text=""),
            ],
            images=[
                ImageSpec(row=3, col="B", data=JPEG, width=160, height=120),
                ImageSpec(row=3, col="C", data=PNG, mime="image/png"),
            ],
        )

    def test_layout_without_floating_images_leaves_anchors_for_dispimg(self) -> None:
        layout = self._layout()
        xlsx = layout_to_xlsx(layout)          # 默认即嵌入单元格模式

        entries = parts(xlsx)
        self.assertFalse(
            [name for name in entries if name.startswith("xl/drawings/")],
            "嵌入单元格模式不应生成浮动图片的 drawing 部件")
        self.assertIsNone(cell_value(xlsx, "改善报告", "B3"))
        self.assertIsNone(cell_value(xlsx, "改善报告", "C3"))

        result = inject_dispimg(
            xlsx,
            [DispImage(cell=f"{p.col}{p.row}", data=p.data,
                       extension="png" if p.mime == "image/png" else "jpeg")
             for p in layout.images],
            sheet_name=layout.sheet_name)

        images = cellimages(result)
        self.assertEqual(len(images), 2)
        ids_b = dispimg_id(formula_of(result, "改善报告", "B3"))
        ids_c = dispimg_id(formula_of(result, "改善报告", "C3"))
        self.assertEqual(images[ids_b], "media/dispimg1.jpeg")
        self.assertEqual(images[ids_c], "media/dispimg2.png")
        self.assertEqual(parts(result)["xl/media/dispimg1.jpeg"], JPEG)
        self.assertEqual(parts(result)["xl/media/dispimg2.png"], PNG)
        # 其余内容与样式仍可被 openpyxl 正常读取
        self.assertEqual(cell_value(result, "改善报告", "A1"), "标题")


if __name__ == "__main__":
    unittest.main(verbosity=2)
