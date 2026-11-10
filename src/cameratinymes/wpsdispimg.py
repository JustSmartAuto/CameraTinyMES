"""WPS `DISPIMG` 嵌入单元格：把图片装进 xlsx 并写成 `=_xlfn.DISPIMG("ID_...",1)`。

为什么必须后处理：openpyxl 不认识 `xl/cellimages.xml`，保存时会丢弃未知部件，
因此流程是「openpyxl 生成工作簿 → 本模块按 zip 级别追加图片与关系 → 改写目标单元格公式」。
结构依据 WPS 表格（ET）的 cellImages 扩展：

- `xl/cellimages.xml`：`etc:cellImages` 下每张图一个 `etc:cellImage`，`xdr:cNvPr/@name` 即图片 ID；
- `xl/_rels/cellimages.xml.rels`：图片 ID → `media/dispimgN.jpeg`；
- `[Content_Types].xml`：新增 cellimages 部件 Override 与 jpeg 扩展名 Default；
- `xl/_rels/workbook.xml.rels`：新增 relationships/cellimages 关系；
- 单元格：`<f>_xlfn.DISPIMG("ID_xxx",1)</f>`，`t="str"`。
"""

from __future__ import annotations

import re
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_ETC = "http://www.wps.cn/officeDocument/2017/etCustomData"
NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

CELLIMAGES_PART = "xl/cellimages.xml"
CELLIMAGES_RELS = "xl/_rels/cellimages.xml.rels"
CELLIMAGES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.cellimages+xml")
CELLIMAGES_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/cellimages")


@dataclass
class DispImage:
    """一张要嵌入单元格的图片。"""

    cell: str                 # 目标单元格，如 "M4"
    data: bytes
    extension: str = "jpeg"   # jpeg / png
    image_id: str = ""        # 缺省自动生成 ID_<32 hex>


def inject_dispimg(xlsx_bytes: bytes, images: list[DispImage],
                   sheet_name: str | None = None) -> bytes:
    """把图片写入 cellimages 并改写目标单元格公式；返回新的 xlsx 字节。"""
    if not images:
        return xlsx_bytes
    with zipfile.ZipFile(BytesIO(xlsx_bytes)) as source:
        entries = {info.filename: source.read(info.filename)
                   for info in source.infolist()}
        order = [info.filename for info in source.infolist()]

    prepared: list[tuple[DispImage, str, str]] = []
    used: set[str] = set()
    for image in images:
        image_id = image.image_id or f"ID_{uuid.uuid4().hex.upper()}"
        while image_id in used:
            image_id = f"ID_{uuid.uuid4().hex.upper()}"
        used.add(image_id)
        prepared.append((image, image_id, f"rId{len(prepared) + 1}"))

    # 1) 媒体文件 + cellimages.xml + 其 rels
    parts: list[str] = []
    rels: list[str] = []
    media: dict[str, bytes] = {}
    for index, (image, image_id, rid) in enumerate(prepared, start=1):
        media_name = f"dispimg{index}.{image.extension}"
        media[f"xl/media/{media_name}"] = image.data
        parts.append(
            "<etc:cellImage><xdr:pic><xdr:nvPicPr>"
            f'<xdr:cNvPr id="{index}" name="{image_id}"/><xdr:cNvPicPr/>'
            "</xdr:nvPicPr><xdr:blipFill>"
            f'<a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch>'
            "</xdr:blipFill><xdr:spPr>"
            '<a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            "</xdr:spPr></xdr:pic></etc:cellImage>"
        )
        rels.append(
            f'<Relationship Id="{rid}" Type="{NS_DOC_REL}/image"'
            f' Target="media/{media_name}"/>'
        )

    entries[CELLIMAGES_PART] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<etc:cellImages xmlns:etc="{NS_ETC}" xmlns:r="{NS_DOC_REL}"'
        f' xmlns:a="{NS_A}" xmlns:xdr="{NS_XDR}">'
        + "".join(parts) + "</etc:cellImages>"
    ).encode("utf-8")
    entries[CELLIMAGES_RELS] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{NS_PKG_REL}">'
        + "".join(rels) + "</Relationships>"
    ).encode("utf-8")
    entries.update(media)

    # 2) [Content_Types].xml：补 cellimages Override 与图片扩展名 Default
    content_types = entries["[Content_Types].xml"].decode("utf-8")
    if f'PartName="/{CELLIMAGES_PART}"' not in content_types:
        content_types = content_types.replace(
            "</Types>",
            f'<Override PartName="/{CELLIMAGES_PART}"'
            f' ContentType="{CELLIMAGES_CONTENT_TYPE}"/></Types>')
    for extension in {image.extension for image, _, _ in prepared}:
        if f'Extension="{extension}"' not in content_types:
            content_types = content_types.replace(
                "</Types>",
                f'<Default Extension="{extension}" ContentType="image/{extension}"/></Types>')
    entries["[Content_Types].xml"] = content_types.encode("utf-8")

    # 3) workbook.xml.rels：补 cellimages 关系
    entries["xl/_rels/workbook.xml.rels"] = _add_workbook_rel(
        entries["xl/_rels/workbook.xml.rels"])

    # 4) 目标工作表：写入 DISPIMG 公式
    target = _sheet_part(entries, sheet_name)
    sheet_xml = entries[target]
    for image, image_id, _ in prepared:
        sheet_xml = _set_cell_formula(
            sheet_xml, image.cell, f'_xlfn.DISPIMG("{image_id}",1)')
    entries[target] = sheet_xml

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target_zip:
        for name in order:
            target_zip.writestr(name, entries[name])
        for name in list(media) + [CELLIMAGES_PART, CELLIMAGES_RELS]:
            if name not in order:
                target_zip.writestr(name, entries[name])
    return buffer.getvalue()


def _add_workbook_rel(xml_bytes: bytes) -> bytes:
    text = xml_bytes.decode("utf-8")
    if CELLIMAGES_REL_TYPE in text:
        return xml_bytes
    existing = set(re.findall(r'Id="([^"]+)"', text))
    rid = "rIdCellImages"
    while rid in existing:
        rid += "X"
    text = text.replace(
        "</Relationships>",
        f'<Relationship Id="{rid}" Type="{CELLIMAGES_REL_TYPE}"'
        ' Target="cellimages.xml"/></Relationships>')
    return text.encode("utf-8")


def _sheet_part(entries: dict[str, bytes], sheet_name: str | None) -> str:
    """按工作表名找到对应的 sheetN.xml（未指定则取第一张）。"""
    workbook = entries.get("xl/workbook.xml", b"").decode("utf-8")
    rels = entries.get("xl/_rels/workbook.xml.rels", b"").decode("utf-8")
    rel_map = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
    sheets = re.findall(
        r'<sheet[^>]*name="([^"]*)"[^>]*r:id="([^"]+)"', workbook, re.DOTALL)
    if not sheets:
        sheets = [(m[1], m[0]) for m in re.findall(
            r'<sheet[^>]*r:id="([^"]+)"[^>]*name="([^"]*)"', workbook, re.DOTALL)]
    chosen = None
    for name, rid in sheets:
        if sheet_name and name == sheet_name:
            chosen = rid
            break
    if chosen is None and sheets:
        chosen = sheets[0][1]
    target = rel_map.get(chosen or "", "worksheets/sheet1.xml")
    if not target.startswith("/"):
        target = "xl/" + target
    else:
        target = target.lstrip("/")
    return target


def _set_cell_formula(xml_bytes: bytes, cell_ref: str, formula: str) -> bytes:
    """把指定单元格改成公式单元格（保留原样式 s 属性）。"""
    ET.register_namespace("", NS_MAIN)
    root = ET.fromstring(xml_bytes)
    row_index = int(re.sub(r"\D", "", cell_ref) or 0)
    row = None
    for candidate in root.iter(f"{{{NS_MAIN}}}row"):
        if candidate.get("r") == str(row_index):
            row = candidate
            break
    if row is None:
        row = ET.SubElement(root, f"{{{NS_MAIN}}}row", {"r": str(row_index)})
    target = None
    for candidate in row.findall(f"{{{NS_MAIN}}}c"):
        if candidate.get("r") == cell_ref:
            target = candidate
            break
    if target is None:
        target = ET.SubElement(row, f"{{{NS_MAIN}}}c", {"r": cell_ref})
    style = target.get("s")
    for child in list(target):
        target.remove(child)
    target.attrib.clear()
    target.set("r", cell_ref)
    if style is not None:
        target.set("s", style)
    target.set("t", "str")
    node = ET.SubElement(target, f"{{{NS_MAIN}}}f")
    node.text = formula
    ET.SubElement(target, f"{{{NS_MAIN}}}v")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
