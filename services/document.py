import os
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from fpdf import FPDF

# ── Unicode sanitiser for Helvetica/Latin-1 PDF ───────────────────────────────
_UNICODE_MAP = str.maketrans({
    '–': '-', '—': '-', '‘': "'", '’': "'",
    '“': '"', '”': '"', '…': '...', '•': '-',
    ' ': ' ', '‐': '-', '‑': '-', '‒': '-', '―': '-',
})

def _s(text) -> str:
    return str(text).translate(_UNICODE_MAP).encode('latin-1', errors='replace').decode('latin-1')

def _trunc(pdf: FPDF, text: str, max_w: float) -> str:
    if pdf.get_string_width(text) <= max_w:
        return text
    while len(text) > 0 and pdf.get_string_width(text + '...') > max_w:
        text = text[:-1]
    return (text + '...') if text else ''


# ── Template colour palettes ──────────────────────────────────────────────────
_PALETTES = {
    "standard": {
        "title":    (30,  16,  79),   # dark purple
        "heading1": (30,  16,  79),
        "heading2": (60,  40, 120),
        "table_hdr":(109, 40, 217),   # violet
        "text":     (50,  50,  50),
        "meta":     (100, 100, 100),
        "divider":  (180, 180, 180),
        "row_even": (245, 243, 255),
        "executive_order": False,     # action items after sections
    },
    "modern": {
        "title":    (0,  105,  92),   # teal
        "heading1": (0,  105,  92),
        "heading2": (0,  137, 123),
        "table_hdr":(0,  150, 136),
        "text":     (40,  40,  40),
        "meta":     (90,  90,  90),
        "divider":  (180, 210, 205),
        "row_even": (230, 247, 245),
        "executive_order": False,
    },
    "executive": {
        "title":    (10,  36,  99),   # navy
        "heading1": (10,  36,  99),
        "heading2": (30,  64, 175),
        "table_hdr":(29,  78, 216),
        "text":     (30,  30,  30),
        "meta":     (80,  80,  80),
        "divider":  (180, 190, 210),
        "row_even": (235, 240, 255),
        "executive_order": True,      # action items BEFORE sections
    },
    "minimal": {
        "title":    (40,  40,  40),
        "heading1": (40,  40,  40),
        "heading2": (80,  80,  80),
        "table_hdr":(100, 100, 100),
        "text":     (70,  70,  70),
        "meta":     (120, 120, 120),
        "divider":  (210, 210, 210),
        "row_even": (245, 245, 245),
        "executive_order": False,
    },
}

def _palette(minutes: dict) -> dict:
    name = (minutes.get("template") or "standard").lower()
    return _PALETTES.get(name, _PALETTES["standard"])


# ── DOCX ──────────────────────────────────────────────────────────────────────

def _docx_heading(doc: Document, text: str, level: int, rgb: tuple):
    p = doc.add_heading(text, level=level)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.runs[0] if p.runs else p.add_run(text)
    run.font.color.rgb = RGBColor(*rgb)


def _docx_action_table(doc: Document, action_items: list, pal: dict):
    if not action_items:
        doc.add_paragraph('No action items recorded.')
        return
    tbl = doc.add_table(rows=len(action_items) + 1, cols=3)
    tbl.style = 'Table Grid'
    for j, h in enumerate(['Action Item', 'Owner', 'Deadline']):
        cell = tbl.rows[0].cells[j]
        cell.text = h
        if cell.paragraphs[0].runs:
            run = cell.paragraphs[0].runs[0]
            run.bold = True
            run.font.color.rgb = RGBColor(255, 255, 255)
        # shade header cell
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement('w:shd')
        r, g, b = pal["table_hdr"]
        shd.set(qn('w:fill'), f'{r:02X}{g:02X}{b:02X}')
        shd.set(qn('w:color'), 'auto')
        shd.set(qn('w:val'), 'clear')
        tc_pr.append(shd)
    for i, item in enumerate(action_items, 1):
        row = tbl.rows[i]
        row.cells[0].text = item.get('task', '')
        row.cells[1].text = item.get('owner', '')
        row.cells[2].text = item.get('deadline', '')


def generate_docx(minutes: dict, output_path: str) -> None:
    pal  = _palette(minutes)
    doc  = Document()
    exec_order = pal["executive_order"]

    for section in doc.sections:
        section.top_margin    = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin   = Inches(1.2)
        section.right_margin  = Inches(1.2)

    # Title
    title_para = doc.add_heading(minutes.get('title', 'Meeting Minutes'), level=0)
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if title_para.runs:
        title_para.runs[0].font.color.rgb = RGBColor(*pal["title"])
    doc.add_paragraph()

    # Metadata table
    meta = minutes.get('metadata', {})
    table = doc.add_table(rows=3, cols=2)
    table.style = 'Table Grid'
    for i, (label, value) in enumerate([
        ('Date',         meta.get('date', 'Unknown')),
        ('Duration',     meta.get('duration', 'Unknown')),
        ('Participants', ', '.join(meta.get('participants', []))),
    ]):
        row = table.rows[i]
        row.cells[0].text = label
        row.cells[1].text = value
        for cell in row.cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(10)
        if row.cells[0].paragraphs[0].runs:
            row.cells[0].paragraphs[0].runs[0].bold = True
    doc.add_paragraph()

    action_items = minutes.get('actionItems', [])

    if exec_order:
        # Executive template: action items first
        _docx_heading(doc, 'Action Items', 1, pal["heading1"])
        _docx_action_table(doc, action_items, pal)
        doc.add_paragraph()
        _docx_heading(doc, 'Discussion', 1, pal["heading1"])
    else:
        _docx_heading(doc, 'Minutes', 1, pal["heading1"])

    for i, section in enumerate(minutes.get('sections', []), 1):
        _docx_heading(doc, f"{i}. {section.get('title', '')}", 2, pal["heading2"])
        p = doc.add_paragraph(section.get('content', ''))
        p.style.font.size = Pt(11)
        doc.add_paragraph()

    if not exec_order:
        _docx_heading(doc, 'Action Items', 1, pal["heading1"])
        _docx_action_table(doc, action_items, pal)

    doc.save(output_path)


# ── PDF ───────────────────────────────────────────────────────────────────────

def _mc(pdf: FPDF, h: float, txt: str, **kwargs):
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, h, txt, **kwargs)


def _pdf_section_heading(pdf: FPDF, text: str, pal: dict):
    pdf.set_font('Helvetica', 'B', 11)
    pdf.set_text_color(*pal["heading2"])
    _mc(pdf, 7, _s(text))


def _pdf_action_table(pdf: FPDF, action_items: list, pal: dict):
    if not action_items:
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(*pal["meta"])
        _mc(pdf, 7, 'No action items recorded.')
        return

    pw   = pdf.epw
    c0   = round(pw * 0.50, 2)
    c1   = round(pw * 0.27, 2)
    c2   = round(pw - c0 - c1, 2)
    col_w = [c0, c1, c2]

    pdf.set_x(pdf.l_margin)
    pdf.set_fill_color(*pal["table_hdr"])
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 9)
    for header, w in zip(['Action Item', 'Owner', 'Deadline'], col_w):
        pdf.cell(w, 8, header, border=1, fill=True)
    pdf.ln()

    re, re_g, re_b = pal["row_even"]
    pdf.set_font('Helvetica', '', 9)
    for idx, item in enumerate(action_items):
        pdf.set_x(pdf.l_margin)
        if idx % 2 == 0:
            pdf.set_fill_color(re, re_g, re_b)
        else:
            pdf.set_fill_color(255, 255, 255)
        pdf.set_text_color(*pal["text"])
        task     = _trunc(pdf, _s(item.get('task', '')),     col_w[0] - 2)
        owner    = _trunc(pdf, _s(item.get('owner', '')),    col_w[1] - 2)
        deadline = _trunc(pdf, _s(item.get('deadline', '')), col_w[2] - 2)
        pdf.cell(col_w[0], 7, task,     border=1, fill=True)
        pdf.cell(col_w[1], 7, owner,    border=1, fill=True)
        pdf.cell(col_w[2], 7, deadline, border=1, fill=True)
        pdf.ln()


def generate_pdf(minutes: dict, output_path: str) -> None:
    pal  = _palette(minutes)
    exec_order = pal["executive_order"]

    pdf = FPDF()
    pdf.set_margins(15, 20, 15)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    # Title
    pdf.set_font('Helvetica', 'B', 20)
    pdf.set_text_color(*pal["title"])
    _mc(pdf, 10, _s(minutes.get('title', 'Meeting Minutes')), align='C')
    pdf.ln(4)

    # Metadata line
    meta = minutes.get('metadata', {})
    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(*pal["meta"])
    parts = []
    if meta.get('date'):        parts.append(f"Date: {meta['date']}")
    if meta.get('duration'):    parts.append(f"Duration: {meta['duration']}")
    if meta.get('participants'): parts.append(f"Participants: {', '.join(meta['participants'])}")
    if parts:
        _mc(pdf, 6, _s('   |   '.join(parts)), align='C')
    pdf.ln(6)

    # Divider
    pdf.set_draw_color(*pal["divider"])
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)

    action_items = minutes.get('actionItems', [])

    if exec_order:
        # Executive: action items block first
        pdf.set_font('Helvetica', 'B', 13)
        pdf.set_text_color(*pal["heading1"])
        _mc(pdf, 8, 'Action Items')
        pdf.ln(2)
        _pdf_action_table(pdf, action_items, pal)
        pdf.ln(6)
        pdf.set_draw_color(*pal["divider"])
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(6)
        pdf.set_font('Helvetica', 'B', 13)
        pdf.set_text_color(*pal["heading1"])
        _mc(pdf, 8, 'Discussion')
    else:
        pdf.set_font('Helvetica', 'B', 13)
        pdf.set_text_color(*pal["heading1"])
        _mc(pdf, 8, 'Minutes')
    pdf.ln(2)

    for i, section in enumerate(minutes.get('sections', []), 1):
        _pdf_section_heading(pdf, f"{i}. {section.get('title', '')}", pal)
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(*pal["text"])
        content = section.get('content', '')
        if content:
            _mc(pdf, 6, _s(content))
        pdf.ln(4)

    if not exec_order:
        pdf.set_font('Helvetica', 'B', 13)
        pdf.set_text_color(*pal["heading1"])
        _mc(pdf, 8, 'Action Items')
        pdf.ln(2)
        _pdf_action_table(pdf, action_items, pal)

    pdf.output(output_path)
