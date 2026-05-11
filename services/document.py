import os
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from fpdf import FPDF

# Helvetica only covers Latin-1. Replace common Unicode punctuation so the
# PDF renderer never sees characters outside that range.
_UNICODE_MAP = str.maketrans({
    '–': '-',   # en dash
    '—': '-',   # em dash
    '‘': "'",   # left single quote
    '’': "'",   # right single quote
    '“': '"',   # left double quote
    '”': '"',   # right double quote
    '…': '...',  # ellipsis
    '•': '-',   # bullet
    ' ': ' ',   # non-breaking space
    '‐': '-',   # hyphen
    '‑': '-',   # non-breaking hyphen
    '‒': '-',   # figure dash
    '―': '-',   # horizontal bar
})

def _s(text) -> str:
    """Sanitize text for Helvetica / Latin-1 PDF output."""
    sanitized = str(text).translate(_UNICODE_MAP)
    # Drop any remaining characters outside Latin-1
    return sanitized.encode('latin-1', errors='replace').decode('latin-1')


def _trunc(pdf: FPDF, text: str, max_w: float) -> str:
    """Truncate text with ellipsis to fit within max_w mm at current font."""
    if pdf.get_string_width(text) <= max_w:
        return text
    while len(text) > 0 and pdf.get_string_width(text + '...') > max_w:
        text = text[:-1]
    return (text + '...') if text else ''


def _add_heading(doc: Document, text: str, level: int):
    p = doc.add_heading(text, level=level)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.runs[0] if p.runs else p.add_run(text)
    run.font.color.rgb = RGBColor(0x1e, 0x10, 0x4f)


def generate_docx(minutes: dict, output_path: str) -> None:
    doc = Document()

    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1.2)
        section.right_margin = Inches(1.2)

    title_para = doc.add_heading(minutes.get('title', 'Meeting Minutes'), level=0)
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if title_para.runs:
        title_para.runs[0].font.color.rgb = RGBColor(0x1e, 0x10, 0x4f)

    doc.add_paragraph()

    meta = minutes.get('metadata', {})
    table = doc.add_table(rows=3, cols=2)
    table.style = 'Table Grid'
    cells = [
        ('Date', meta.get('date', 'Unknown')),
        ('Duration', meta.get('duration', 'Unknown')),
        ('Participants', ', '.join(meta.get('participants', []))),
    ]
    for i, (label, value) in enumerate(cells):
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

    _add_heading(doc, 'Minutes', level=1)
    for i, section in enumerate(minutes.get('sections', []), 1):
        _add_heading(doc, f"{i}. {section.get('title', '')}", level=2)
        p = doc.add_paragraph(section.get('content', ''))
        p.style.font.size = Pt(11)
        doc.add_paragraph()

    _add_heading(doc, 'Action Items', level=1)
    action_items = minutes.get('actionItems', [])
    if action_items:
        tbl = doc.add_table(rows=len(action_items) + 1, cols=3)
        tbl.style = 'Table Grid'
        for j, h in enumerate(['Action Item', 'Owner', 'Deadline']):
            cell = tbl.rows[0].cells[j]
            cell.text = h
            if cell.paragraphs[0].runs:
                cell.paragraphs[0].runs[0].bold = True
        for i, item in enumerate(action_items, 1):
            row = tbl.rows[i]
            row.cells[0].text = item.get('task', '')
            row.cells[1].text = item.get('owner', '')
            row.cells[2].text = item.get('deadline', '')
    else:
        doc.add_paragraph('No action items recorded.')

    doc.save(output_path)


def _mc(pdf: FPDF, h: float, txt: str, **kwargs):
    """multi_cell that always starts at the left margin — prevents x-drift."""
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, h, txt, **kwargs)


def generate_pdf(minutes: dict, output_path: str) -> None:
    pdf = FPDF()
    pdf.set_margins(15, 20, 15)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    # Title
    pdf.set_font('Helvetica', 'B', 20)
    pdf.set_text_color(30, 16, 79)
    _mc(pdf, 10, _s(minutes.get('title', 'Meeting Minutes')), align='C')
    pdf.ln(4)

    # Metadata
    meta = minutes.get('metadata', {})
    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(100, 100, 100)
    parts = []
    if meta.get('date'):
        parts.append(f"Date: {meta['date']}")
    if meta.get('duration'):
        parts.append(f"Duration: {meta['duration']}")
    participants = meta.get('participants', [])
    if participants:
        parts.append(f"Participants: {', '.join(participants)}")
    if parts:
        _mc(pdf, 6, _s('   |   '.join(parts)), align='C')
    pdf.ln(6)

    # Divider
    pdf.set_draw_color(180, 180, 180)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)

    # Sections
    pdf.set_font('Helvetica', 'B', 13)
    pdf.set_text_color(30, 16, 79)
    _mc(pdf, 8, 'Minutes')
    pdf.ln(2)

    for i, section in enumerate(minutes.get('sections', []), 1):
        pdf.set_font('Helvetica', 'B', 11)
        pdf.set_text_color(60, 40, 120)
        _mc(pdf, 7, _s(f"{i}. {section.get('title', '')}"))
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(50, 50, 50)
        content = section.get('content', '')
        if content:
            _mc(pdf, 6, _s(content))
        pdf.ln(4)

    # Action items
    pdf.set_font('Helvetica', 'B', 13)
    pdf.set_text_color(30, 16, 79)
    _mc(pdf, 8, 'Action Items')
    pdf.ln(2)

    action_items = minutes.get('actionItems', [])
    if action_items:
        pw = pdf.epw
        # Pin last column so widths sum exactly to pw (avoids float overflow)
        c0 = round(pw * 0.50, 2)
        c1 = round(pw * 0.27, 2)
        c2 = round(pw - c0 - c1, 2)
        col_w = [c0, c1, c2]

        # Header row
        pdf.set_x(pdf.l_margin)
        pdf.set_fill_color(109, 40, 217)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font('Helvetica', 'B', 9)
        for header, w in zip(['Action Item', 'Owner', 'Deadline'], col_w):
            pdf.cell(w, 8, header, border=1, fill=True)
        pdf.ln()

        # Data rows
        pdf.set_font('Helvetica', '', 9)
        for idx, item in enumerate(action_items):
            pdf.set_x(pdf.l_margin)
            pdf.set_fill_color(245, 243, 255 if idx % 2 == 0 else 255)
            pdf.set_text_color(30, 30, 30)
            task     = _trunc(pdf, _s(item.get('task', '')),     col_w[0] - 2)
            owner    = _trunc(pdf, _s(item.get('owner', '')),    col_w[1] - 2)
            deadline = _trunc(pdf, _s(item.get('deadline', '')), col_w[2] - 2)
            pdf.cell(col_w[0], 7, task,     border=1, fill=True)
            pdf.cell(col_w[1], 7, owner,    border=1, fill=True)
            pdf.cell(col_w[2], 7, deadline, border=1, fill=True)
            pdf.ln()
    else:
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(120, 120, 120)
        _mc(pdf, 7, 'No action items recorded.')

    pdf.output(output_path)
