"""外部文件导入：把 CSV / Excel 表格变成系统里的学生和快递。

设计要点
--------
1. 零第三方依赖 —— xlsx 直接用 zipfile + ElementTree 解析（xlsx 本质就是 zip
   包着的 XML），避免在客户机/树莓派上装 openpyxl。
2. 列名自动映射 —— 用户导出的表头千奇百怪（"手机号"/"电话"/"phone"/"联系方式"），
   这里用别名表兜住常见写法，映射不上就让用户在网页上手动指定列。
3. 只解析不落库 —— 解析结果先返回给前端预览，用户点"确认导入"才写数据库。
"""
import csv
import io
import os
import re
import zipfile
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# 列别名 → 标准字段
FIELD_ALIASES = {
    "name": ["姓名", "学生姓名", "名字", "name", "学生", "收件人", "收件人姓名", "客户姓名"],
    "student_no": ["学号", "编号", "学籍号", "student_no", "student id", "studentid", "工号", "编号/学号"],
    "phone": ["手机号", "手机", "电话", "联系电话", "联系方式", "phone", "mobile", "tel", "手机号码"],
    "epc": ["epc", "卡号", "rfid", "rfid_epc", "标签", "电子标签", "卡号/epc"],
    "pickup_code": ["取件码", "取件号", "取货号", "取件编号", "pickup_code", "code", "货架码", "取件码/货架号"],
    "tracking_no": ["运单号", "快递单号", "单号", "tracking_no", "tracking", "物流单号", "快递号", "运单编号"],
    "shelf": ["货架", "位置", "货架号", "shelf", "存放位置"],
}

REQUIRED_FIELDS = {
    "students": ["name", "phone"],
    "packages": ["phone", "pickup_code"],
}

MAX_ROWS = 20000


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-/()（）:：]+", "", (s or "").strip().lower())


def _detect_field(header: str) -> str | None:
    h = _norm(header)
    if not h:
        return None
    for field, aliases in FIELD_ALIASES.items():
        for a in aliases:
            if _norm(a) == h:
                return field
    # 退一步：包含匹配（"学生手机号" 也能命中 phone）
    for field, aliases in FIELD_ALIASES.items():
        for a in aliases:
            na = _norm(a)
            if len(na) >= 2 and na in h:
                return field
    return None


# ==================== 文件解析 ====================

def _col_index(ref: str) -> int:
    m = re.match(r"([A-Za-z]+)", ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(file_bytes: bytes) -> tuple[list[list[str]], list[str]]:
    """返回 (第一个有数据的工作表的二维数据, 所有工作表名)。"""
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
        names = z.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(NS + "t")))

        # 按 workbook.xml 里的顺序拿 sheet 文件
        sheet_files: list[tuple[str, str]] = []
        wb_path = "xl/workbook.xml"
        if wb_path in names:
            wb = ET.fromstring(z.read(wb_path))
            rels = {}
            if "xl/_rels/workbook.xml.rels" in names:
                rr = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
                for rel in rr:
                    rels[rel.get("Id")] = rel.get("Target")
            for sh in wb.iter(NS + "sheet"):
                target = rels.get(sh.get(RNS + "id"), "")
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
                sheet_files.append((sh.get("name") or "Sheet", target))

        if not sheet_files:
            sheet_files = [(n, n) for n in sorted(names) if n.startswith("xl/worksheets/sheet")]

        sheet_names = [s[0] for s in sheet_files]
        for sname, path in sheet_files:
            if path not in names:
                continue
            root = ET.fromstring(z.read(path))
            rows: list[list[str]] = []
            for row in root.iter(NS + "row"):
                cells: dict[int, str] = {}
                for c in row.findall(NS + "c"):
                    t = c.get("t")
                    v = c.find(NS + "v")
                    isel = c.find(NS + "is")
                    if t == "s" and v is not None and v.text is not None:
                        idx = int(v.text)
                        val = shared[idx] if idx < len(shared) else ""
                    elif t == "inlineStr" and isel is not None:
                        val = "".join(x.text or "" for x in isel.iter(NS + "t"))
                    elif v is not None:
                        val = v.text or ""
                    else:
                        val = ""
                    cells[_col_index(c.get("r"))] = (val or "").strip()
                if not cells:
                    rows.append([])
                    continue
                width = max(cells.keys()) + 1
                rows.append([cells.get(i, "") for i in range(width)])
            # 去掉尾部空行
            while rows and not any(x.strip() for x in rows[-1]):
                rows.pop()
            if rows:
                return rows, sheet_names
        return [], sheet_names


def read_csv(file_bytes: bytes) -> list[list[str]]:
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法识别文件编码，请另存为 UTF-8 的 CSV")
    delim = "\t" if text.count("\t") > text.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    while rows and not any(x.strip() for x in rows[-1]):
        rows.pop()
    return rows


def parse_upload(filename: str, file_bytes: bytes) -> tuple[list[list[str]], list[str]]:
    ext = os.path.splitext(filename.lower())[1]
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(file_bytes)
    if ext == ".xls":
        raise ValueError("不支持老版 .xls，请在 Excel 里另存为 .xlsx 或 .csv")
    if ext in (".csv", ".txt", ""):
        return read_csv(file_bytes), []
    raise ValueError(f"不支持的文件类型 {ext}，请上传 .xlsx 或 .csv")


# ==================== 表头映射 ====================

def build_mapping(rows: list[list[str]]) -> dict[str, int]:
    """从表头行推断 {字段: 列号}。找不到表头就用第一行的值兜底猜。"""
    if not rows:
        return {}
    for r in rows[:5]:
        mapping: dict[str, int] = {}
        for i, cell in enumerate(r):
            f = _detect_field(cell)
            if f and f not in mapping:
                mapping[f] = i
        if len(mapping) >= 2:
            return mapping
    return {}


def guess_kind(mapping: dict[str, int]) -> str:
    if "pickup_code" in mapping:
        return "packages"
    if "name" in mapping or "student_no" in mapping:
        return "students"
    return "students"


def extract_rows(rows: list[list[str]], mapping: dict[str, int]) -> tuple[list[dict], int]:
    """按 mapping 把原始二维表转成 [{字段: 值}]，并跳过表头行。"""
    if not rows:
        return [], 0
    header_idx = 0
    for i, r in enumerate(rows[:5]):
        m = {}
        for j, cell in enumerate(r):
            f = _detect_field(cell)
            if f and f not in m:
                m[f] = j
        if len(m) >= 2:
            header_idx = i
            break

    out: list[dict] = []
    for r in rows[header_idx + 1: header_idx + 1 + MAX_ROWS]:
        rec = {}
        for field, col in mapping.items():
            rec[field] = (r[col] if col < len(r) else "").strip()
        if not any(rec.values()):
            continue
        out.append(rec)
    return out, header_idx


# ==================== 校验 ====================

def clean_phone(p: str) -> str:
    return re.sub(r"[^\d]", "", p or "")


def validate(records: list[dict], kind: str) -> tuple[list[dict], list[dict]]:
    """返回 (合法记录, 错误记录)。"""
    ok, bad = [], []
    need = REQUIRED_FIELDS.get(kind, [])
    seen_code = set()
    for i, rec in enumerate(records):
        line = i + 1
        phone = clean_phone(rec.get("phone", ""))
        rec["phone"] = phone
        errs = []
        for f in need:
            if not (rec.get(f) or "").strip():
                errs.append(f"缺少{ {'name':'姓名','phone':'手机号','pickup_code':'取件码'}.get(f, f) }")
        if phone and not re.fullmatch(r"1\d{10}", phone):
            errs.append("手机号格式不正确（应为 11 位）")
        if kind == "packages" and rec.get("pickup_code"):
            key = rec["pickup_code"].strip().upper()
            if key in seen_code:
                errs.append("文件内取件码重复")
            seen_code.add(key)
        if errs:
            bad.append({"line": line, "row": rec, "errors": errs})
        else:
            ok.append({"line": line, "row": rec})
    return ok, bad
