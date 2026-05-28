"""
Meeting-minutes document generator.
Produces styled .docx and .pdf files from the structured minutes dict returned
by formatting.py.  Supports four visual templates: standard, modern, executive,
minimal.
"""
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from fpdf import FPDF

# ── Unicode → Latin-1 sanitiser (fpdf uses Helvetica / Latin-1) ──────────────
_UNICODE_MAP = str.maketrans({
    '–': '-',  '—': '-',  '‘': "'", '’': "'",
    '“': '"',  '”': '"',  '…': '...','•': '-',
    ' ': ' ',  '‐': '-',  '‑': '-', '‒': '-',
    '―': '-',  '→': '->',
})

def _s(text) -> str:
    return str(text).translate(_UNICODE_MAP).encode('latin-1', errors='replace').decode('latin-1')

def _trunc(pdf: 'FPDF', text: str, max_w: float) -> str:
    if pdf.get_string_width(text) <= max_w:
        return text
    while text and pdf.get_string_width(text + '...') > max_w:
        text = text[:-1]
    return (text + '...') if text else ''


# ── Colour palettes ───────────────────────────────────────────────────────────
_PALETTES = {
    "standard": {
        "banner":    (45,  18, 115),
        "banner_sub":(80,  50, 160),
        "title":     (30,  16,  79),
        "h1":        (30,  16,  79),
        "h2":        (60,  40, 120),
        "h3":        (90,  60, 160),
        "tbl_hdr":   (109, 40, 217),
        "accent_bg": (245, 243, 255),
        "text":      (45,  45,  55),
        "meta":      (100, 100, 120),
        "divider":   (200, 190, 230),
        "row_even":  (248, 246, 255),
        "executive_order": False,
    },
    "modern": {
        "banner":    (0,   75,  68),
        "banner_sub":(0,  110, 100),
        "title":     (0,  105,  92),
        "h1":        (0,  105,  92),
        "h2":        (0,  137, 123),
        "h3":        (0,  155, 140),
        "tbl_hdr":   (0,  150, 136),
        "accent_bg": (235, 252, 249),
        "text":      (35,  40,  40),
        "meta":      (85,  95,  90),
        "divider":   (170, 215, 210),
        "row_even":  (228, 246, 243),
        "executive_order": False,
    },
    "executive": {
        "banner":    (8,   28,  80),
        "banner_sub":(20,  55, 130),
        "title":     (10,  36,  99),
        "h1":        (10,  36,  99),
        "h2":        (30,  64, 175),
        "h3":        (55,  90, 210),
        "tbl_hdr":   (29,  78, 216),
        "accent_bg": (232, 239, 255),
        "text":      (25,  30,  40),
        "meta":      (75,  85, 105),
        "divider":   (175, 190, 220),
        "row_even":  (233, 239, 255),
        "executive_order": True,
    },
    "minimal": {
        "banner":    (45,  45,  45),
        "banner_sub":(75,  75,  75),
        "title":     (35,  35,  35),
        "h1":        (35,  35,  35),
        "h2":        (65,  65,  65),
        "h3":        (95,  95,  95),
        "tbl_hdr":   (95,  95,  95),
        "accent_bg": (248, 248, 248),
        "text":      (65,  65,  65),
        "meta":      (115, 115, 115),
        "divider":   (210, 210, 210),
        "row_even":  (244, 244, 244),
        "executive_order": False,
    },
}

_PRIORITY_COLORS = {
    "high":   ((185, 28,  28),  (254, 226, 226)),   # text, bg
    "medium": ((146, 64,  14),  (254, 243, 199)),
    "low":    ((75,  85,  99),  (241, 245, 249)),
}
_STATUS_COLORS = {
    "answered":   ((6,  95,  70),  (209, 250, 229)),
    "unresolved": ((185, 28, 28),  (254, 226, 226)),
    "deferred":   ((120, 53, 15),  (254, 243, 199)),
}

def _palette(minutes: dict) -> dict:
    name = (minutes.get("template") or "standard").lower()
    return _PALETTES.get(name, _PALETTES["standard"])


# ═══════════════════════════════════════════════════════════════════════════════
#  DOCX
# ═══════════════════════════════════════════════════════════════════════════════

def _shade_cell(cell, hex_color: str):
    """Apply a solid background fill to a table cell."""
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:fill'), hex_color)
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:val'), 'clear')
    tc_pr.append(shd)


def _rgb_to_hex(rgb: tuple) -> str:
    return '{:02X}{:02X}{:02X}'.format(*rgb)


def _docx_h1(doc, text: str, pal: dict):
    p = doc.add_heading(text, level=1)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.runs[0] if p.runs else p.add_run(text)
    run.font.color.rgb = RGBColor(*pal["h1"])
    run.font.size = Pt(14)


def _docx_h2(doc, text: str, pal: dict):
    p = doc.add_heading(text, level=2)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = p.runs[0] if p.runs else p.add_run(text)
    run.font.color.rgb = RGBColor(*pal["h2"])
    run.font.size = Pt(12)


def _docx_bullet(doc, text: str, indent_level=0):
    p = doc.add_paragraph(style='List Bullet')
    p.paragraph_format.left_indent = Inches(0.25 * (indent_level + 1))
    run = p.add_run(text)
    run.font.size = Pt(10)


def _docx_body(doc, text: str, pal: dict):
    p = doc.add_paragraph(text)
    p.paragraph_format.space_after = Pt(4)
    for run in p.runs:
        run.font.size = Pt(10.5)
        run.font.color.rgb = RGBColor(*pal["text"])


def _docx_info_row(doc, label: str, value: str, pal: dict):
    p = doc.add_paragraph()
    lb = p.add_run(f'{label}: ')
    lb.bold = True
    lb.font.size = Pt(10)
    lb.font.color.rgb = RGBColor(*pal["h2"])
    vl = p.add_run(value)
    vl.font.size = Pt(10)
    vl.font.color.rgb = RGBColor(*pal["text"])


def _docx_action_table(doc, action_items: list, pal: dict):
    if not action_items:
        doc.add_paragraph('No action items recorded.')
        return

    headers = ['Action Item', 'Owner', 'Deadline', 'Priority']
    col_count = len(headers)
    tbl = doc.add_table(rows=len(action_items) + 1, cols=col_count)
    tbl.style = 'Table Grid'

    # Header row
    hdr_hex = _rgb_to_hex(pal["tbl_hdr"])
    for j, h in enumerate(headers):
        cell = tbl.rows[0].cells[j]
        cell.text = h
        if cell.paragraphs[0].runs:
            run = cell.paragraphs[0].runs[0]
            run.bold = True
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(255, 255, 255)
        _shade_cell(cell, hdr_hex)

    # Data rows
    priority_hex = {
        k: (_rgb_to_hex(v[1]), _rgb_to_hex((int((v[0][0]+255)/2), int((v[0][1]+255)/2), int((v[0][2]+255)/2))))
        for k, v in _PRIORITY_COLORS.items()
    }
    for i, item in enumerate(action_items, 1):
        row = tbl.rows[i]
        prio = (item.get('priority') or 'low').lower()
        cells_data = [
            item.get('task', ''),
            item.get('owner', 'TBD'),
            item.get('deadline', 'TBD'),
            prio.upper(),
        ]
        for j, val in enumerate(cells_data):
            cell = row.cells[j]
            cell.text = val
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(9)
            if j == 3 and prio in _PRIORITY_COLORS:
                txt_rgb, bg_rgb = _PRIORITY_COLORS[prio]
                _shade_cell(cell, _rgb_to_hex(bg_rgb))
                if cell.paragraphs[0].runs:
                    cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(*txt_rgb)
                    cell.paragraphs[0].runs[0].bold = True


def generate_docx(minutes: dict, output_path: str) -> None:
    pal = _palette(minutes)
    doc = Document()
    exec_order = pal["executive_order"]

    # Page margins
    for section in doc.sections:
        section.top_margin    = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin   = Inches(1.15)
        section.right_margin  = Inches(1.15)

    # ── Title ────────────────────────────────────────────────────────────────
    title_para = doc.add_heading(minutes.get('title', 'Meeting Minutes'), level=0)
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if title_para.runs:
        title_para.runs[0].font.color.rgb = RGBColor(*pal["title"])
        title_para.runs[0].font.size = Pt(22)
    doc.add_paragraph()

    # ── Metadata ─────────────────────────────────────────────────────────────
    meta = minutes.get('metadata', {})
    participants = meta.get('participants', [])
    _docx_info_row(doc, 'Date',         meta.get('date', 'Unknown'), pal)
    _docx_info_row(doc, 'Duration',     meta.get('duration', 'Unknown'), pal)
    _docx_info_row(doc, 'Participants', ', '.join(participants) if participants else 'Unknown', pal)
    doc.add_paragraph()

    # ── Overview ─────────────────────────────────────────────────────────────
    overview = minutes.get('overview', '')
    if overview:
        _docx_h1(doc, 'Overview', pal)
        _docx_body(doc, overview, pal)
        doc.add_paragraph()

    action_items = minutes.get('actionItems', [])

    if exec_order:
        # Executive: action items before detailed discussion
        _docx_h1(doc, 'Action Items', pal)
        _docx_action_table(doc, action_items, pal)
        doc.add_paragraph()

    # ── Key Decisions ────────────────────────────────────────────────────────
    key_decisions = minutes.get('keyDecisions', [])
    if key_decisions:
        _docx_h1(doc, 'Key Decisions', pal)
        for i, d in enumerate(key_decisions, 1):
            p = doc.add_paragraph()
            num = p.add_run(f'{i}. ')
            num.bold = True
            num.font.size = Pt(10.5)
            num.font.color.rgb = RGBColor(*pal["h2"])
            dec = p.add_run(d.get('decision', ''))
            dec.bold = True
            dec.font.size = Pt(10.5)
            dec.font.color.rgb = RGBColor(*pal["text"])
            if d.get('made_by'):
                by = doc.add_paragraph()
                r = by.add_run(f"  Proposed by: {d['made_by']}")
                r.italic = True
                r.font.size = Pt(9.5)
                r.font.color.rgb = RGBColor(*pal["meta"])
            if d.get('context'):
                ctx = doc.add_paragraph()
                r = ctx.add_run(f"  {d['context']}")
                r.font.size = Pt(10)
                r.font.color.rgb = RGBColor(*pal["text"])
            p.paragraph_format.space_after = Pt(2)
        doc.add_paragraph()

    # ── Discussion Topics ────────────────────────────────────────────────────
    sections = minutes.get('sections', [])
    if sections:
        _docx_h1(doc, 'Topics Discussed', pal)
        for sec in sections:
            _docx_h2(doc, sec.get('title', ''), pal)
            summary = sec.get('summary', '')
            if summary:
                _docx_body(doc, summary, pal)
            for pt in sec.get('key_points', []):
                _docx_bullet(doc, pt)
            doc.add_paragraph()

    # ── Action Items (non-executive order) ───────────────────────────────────
    if not exec_order:
        _docx_h1(doc, 'Action Items', pal)
        _docx_action_table(doc, action_items, pal)
        doc.add_paragraph()

    # ── Speaker Summaries ────────────────────────────────────────────────────
    speaker_summaries = minutes.get('speakerSummaries', [])
    if speaker_summaries:
        _docx_h1(doc, 'Speaker Summaries', pal)
        for spk in speaker_summaries:
            p = doc.add_paragraph()
            r = p.add_run(spk.get('speaker', ''))
            r.bold = True
            r.font.size = Pt(11)
            r.font.color.rgb = RGBColor(*pal["h2"])
            role = spk.get('role', '')
            if role:
                p.add_run(f'  —  {role}').font.size = Pt(10)
            for contrib in spk.get('key_contributions', []):
                _docx_bullet(doc, contrib)
            doc.add_paragraph()

    # ── Questions Raised ─────────────────────────────────────────────────────
    questions = minutes.get('questionsRaised', [])
    if questions:
        _docx_h1(doc, 'Questions Raised', pal)
        for q in questions:
            status = (q.get('status') or 'unresolved').lower()
            p = doc.add_paragraph()
            r = p.add_run(f"Q: {q.get('question', '')}")
            r.bold = True
            r.font.size = Pt(10.5)
            r.font.color.rgb = RGBColor(*pal["text"])
            status_run = p.add_run(f"  [{status.upper()}]")
            status_run.bold = True
            status_run.font.size = Pt(9)
            if status in _STATUS_COLORS:
                status_run.font.color.rgb = RGBColor(*_STATUS_COLORS[status][0])
            if q.get('asked_by'):
                p2 = doc.add_paragraph()
                r2 = p2.add_run(f"  Asked by: {q['asked_by']}")
                r2.italic = True
                r2.font.size = Pt(9.5)
                r2.font.color.rgb = RGBColor(*pal["meta"])
            if q.get('answer_summary'):
                p3 = doc.add_paragraph()
                r3 = p3.add_run(f"  {q['answer_summary']}")
                r3.font.size = Pt(10)
                r3.font.color.rgb = RGBColor(*pal["text"])
            doc.add_paragraph()

    # ── Next Steps ───────────────────────────────────────────────────────────
    next_steps = minutes.get('nextSteps', [])
    if next_steps:
        _docx_h1(doc, 'Next Steps', pal)
        for step in next_steps:
            p = doc.add_paragraph()
            r = p.add_run(f'→  {step}')
            r.font.size = Pt(10.5)
            r.font.color.rgb = RGBColor(*pal["text"])
        doc.add_paragraph()

    doc.save(output_path)


# ═══════════════════════════════════════════════════════════════════════════════
#  PDF helpers
# ═══════════════════════════════════════════════════════════════════════════════

class _PDF(FPDF):
    """FPDF subclass with convenience methods."""

    def __init__(self, pal):
        super().__init__()
        self.pal = pal

    # ── Low-level primitives ─────────────────────────────────────────────────

    def mc(self, h: float, txt: str, indent: float = 0, **kw):
        self.set_x(self.l_margin + indent)
        self.multi_cell(self.epw - indent, h, _s(txt), **kw)

    def ln_gap(self, h: float = 4):
        self.ln(h)

    def divider(self):
        self.set_draw_color(*self.pal["divider"])
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln_gap(5)

    # ── Shaded box ───────────────────────────────────────────────────────────

    def info_box(self, lines: list[str], pad: float = 4):
        """Draw a lightly shaded rectangle around a block of text lines."""
        bg = self.pal["accent_bg"]
        start_y = self.get_y()
        # Render text to measure height
        self.set_font('Helvetica', '', 10)
        self.set_text_color(*self.pal["text"])
        # Estimate height (we'll draw the rect after)
        line_h = 6
        approx_h = len(lines) * line_h + pad * 2

        self.set_fill_color(*bg)
        self.set_draw_color(*self.pal["divider"])
        self.rect(self.l_margin, start_y, self.epw, approx_h + 4, style='FD')
        self.set_y(start_y + pad)

        for ln in lines:
            self.mc(line_h, ln, indent=pad)
        self.ln_gap(pad + 2)

    # ── Section headings ─────────────────────────────────────────────────────

    def h1(self, text: str):
        self.set_font('Helvetica', 'B', 13)
        self.set_text_color(*self.pal["h1"])
        self.mc(8, text)
        self.ln_gap(1)
        self.divider()

    def h2(self, text: str):
        self.set_font('Helvetica', 'B', 11)
        self.set_text_color(*self.pal["h2"])
        self.mc(7, text)
        self.ln_gap(1)

    def h3(self, text: str):
        self.set_font('Helvetica', 'B', 10)
        self.set_text_color(*self.pal["h3"])
        self.mc(6, text)

    def body(self, text: str, indent: float = 0):
        self.set_font('Helvetica', '', 10)
        self.set_text_color(*self.pal["text"])
        self.mc(6, text, indent=indent)

    def meta_text(self, text: str, indent: float = 0):
        self.set_font('Helvetica', 'I', 9)
        self.set_text_color(*self.pal["meta"])
        self.mc(5, text, indent=indent)

    def bullet(self, text: str, indent: float = 5):
        self.set_font('Helvetica', '', 9.5)
        self.set_text_color(*self.pal["text"])
        # Draw dash manually
        self.set_x(self.l_margin + indent)
        bullet_w = self.get_string_width('- ')
        self.set_font('Helvetica', 'B', 10)
        self.set_text_color(*self.pal["h3"])
        self.cell(bullet_w, 5.5, '-')
        self.set_font('Helvetica', '', 9.5)
        self.set_text_color(*self.pal["text"])
        self.multi_cell(self.epw - indent - bullet_w, 5.5, _s(text))

    def badge(self, text: str, txt_rgb: tuple, bg_rgb: tuple):
        """Inline colored badge — renders as a tiny filled rect + text."""
        old_x = self.get_x()
        old_y = self.get_y()
        self.set_font('Helvetica', 'B', 8)
        w = self.get_string_width(text) + 4
        self.set_fill_color(*bg_rgb)
        self.set_draw_color(*txt_rgb)
        self.set_text_color(*txt_rgb)
        self.rect(old_x, old_y + 1, w, 5, style='FD')
        self.set_xy(old_x + 2, old_y + 1)
        self.cell(w - 4, 5, text)
        self.set_xy(old_x + w + 2, old_y)


def _pdf_action_table(pdf: _PDF, action_items: list):
    if not action_items:
        pdf.body('No action items recorded.')
        return

    pal = pdf.pal
    pw  = pdf.epw
    # Column widths: task 42%, owner 22%, deadline 20%, priority 16%
    cw = [round(pw * r, 1) for r in (0.42, 0.22, 0.20, 0.16)]

    # Header row
    pdf.set_x(pdf.l_margin)
    pdf.set_fill_color(*pal["tbl_hdr"])
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 9)
    for hdr, w in zip(['Task', 'Owner', 'Deadline', 'Priority'], cw):
        pdf.cell(w, 8, hdr, border=1, fill=True)
    pdf.ln()

    pdf.set_font('Helvetica', '', 9)
    re_bg = pal["row_even"]
    for idx, item in enumerate(action_items):
        prio = (item.get('priority') or 'low').lower()
        prio_txt, prio_bg = _PRIORITY_COLORS.get(prio, ((75, 85, 99), (241, 245, 249)))

        pdf.set_x(pdf.l_margin)
        row_bg = re_bg if idx % 2 == 0 else (255, 255, 255)

        for col, (val, w) in enumerate(zip([
            item.get('task', ''),
            item.get('owner', 'TBD'),
            item.get('deadline', 'TBD'),
            prio.upper(),
        ], cw)):
            if col == 3:
                pdf.set_fill_color(*prio_bg)
                pdf.set_text_color(*prio_txt)
                pdf.set_font('Helvetica', 'B', 9)
            else:
                pdf.set_fill_color(*row_bg)
                pdf.set_text_color(*pal["text"])
                pdf.set_font('Helvetica', '', 9)
            txt = _trunc(pdf, _s(val), w - 2)
            pdf.cell(w, 7, txt, border=1, fill=True)
        pdf.ln()
    pdf.ln_gap(2)


def generate_pdf(minutes: dict, output_path: str) -> None:
    pal = _palette(minutes)
    exec_order = pal["executive_order"]

    pdf = _PDF(pal)
    pdf.set_margins(15, 18, 15)
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=22)

    # ── Banner ───────────────────────────────────────────────────────────────
    banner_h = 32
    pdf.set_fill_color(*pal["banner"])
    pdf.rect(0, 0, pdf.w, banner_h, style='F')
    # subtle accent strip
    pdf.set_fill_color(*pal["banner_sub"])
    pdf.rect(0, banner_h - 5, pdf.w, 5, style='F')

    pdf.set_y(7)
    pdf.set_font('Helvetica', 'B', 18)
    pdf.set_text_color(255, 255, 255)
    title_str = _s(minutes.get('title', 'Meeting Minutes'))
    pdf.mc(9, title_str, align='C')

    meta = minutes.get('metadata', {})
    meta_parts = []
    if meta.get('date'):        meta_parts.append(meta['date'])
    if meta.get('duration'):    meta_parts.append(meta['duration'])
    participants = meta.get('participants', [])
    if participants:            meta_parts.append(f"{len(participants)} participants")
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(220, 215, 255)
    pdf.mc(5, _s('  |  '.join(meta_parts)), align='C')

    pdf.set_y(banner_h + 8)
    pdf.set_text_color(*pal["text"])

    # ── Participants detail ──────────────────────────────────────────────────
    if participants:
        pdf.set_font('Helvetica', 'B', 9)
        pdf.set_text_color(*pal["meta"])
        pdf.mc(5, _s('Attendees: ' + ', '.join(participants)))
        pdf.ln_gap(3)

    # ── Overview ─────────────────────────────────────────────────────────────
    overview = minutes.get('overview', '')
    if overview:
        pdf.h1('Overview')
        # Wrap overview in a lightly shaded box
        bg = pal["accent_bg"]
        start_y = pdf.get_y()
        pdf.set_font('Helvetica', '', 10)
        pdf.set_text_color(*pal["text"])
        # Measure approximate height
        line_count = pdf.multi_cell(pdf.epw - 8, 6, _s(overview), dry_run=True, output='LINES')
        box_h = len(line_count) * 6 + 8
        pdf.set_fill_color(*bg)
        pdf.set_draw_color(*pal["divider"])
        pdf.rect(pdf.l_margin, start_y, pdf.epw, box_h, style='FD')
        pdf.set_y(start_y + 4)
        pdf.mc(6, overview, indent=4)
        pdf.set_y(start_y + box_h + 4)

    action_items = minutes.get('actionItems', [])

    if exec_order:
        pdf.h1('Action Items')
        _pdf_action_table(pdf, action_items)
        pdf.ln_gap(4)

    # ── Key Decisions ────────────────────────────────────────────────────────
    key_decisions = minutes.get('keyDecisions', [])
    if key_decisions:
        pdf.h1('Key Decisions')
        for i, d in enumerate(key_decisions, 1):
            # Number badge
            pdf.set_x(pdf.l_margin)
            pdf.set_font('Helvetica', 'B', 10)
            pdf.set_text_color(*pal["h2"])
            num_w = 10
            pdf.cell(num_w, 6, f'{i}.')
            pdf.set_font('Helvetica', 'B', 10)
            pdf.set_text_color(*pal["text"])
            pdf.multi_cell(pdf.epw - num_w, 6, _s(d.get('decision', '')))
            if d.get('made_by'):
                pdf.meta_text(f"Proposed by {d['made_by']}", indent=num_w)
            if d.get('context'):
                pdf.body(d['context'], indent=num_w)
            pdf.ln_gap(4)

    # ── Topics Discussed ─────────────────────────────────────────────────────
    sections = minutes.get('sections', [])
    if sections:
        pdf.h1('Topics Discussed')
        for sec in sections:
            pdf.h2(sec.get('title', ''))
            summary = sec.get('summary', '')
            if summary:
                pdf.body(summary)
                pdf.ln_gap(3)
            for pt in sec.get('key_points', []):
                pdf.bullet(pt)
            pdf.ln_gap(5)

    # ── Action Items (non-executive) ─────────────────────────────────────────
    if not exec_order:
        pdf.h1('Action Items')
        _pdf_action_table(pdf, action_items)

    # ── Speaker Summaries ────────────────────────────────────────────────────
    speaker_summaries = minutes.get('speakerSummaries', [])
    if speaker_summaries:
        pdf.h1('Speaker Summaries')
        for spk in speaker_summaries:
            pdf.set_font('Helvetica', 'B', 10.5)
            pdf.set_text_color(*pal["h2"])
            name_str = _s(spk.get('speaker', ''))
            role_str = _s(spk.get('role', ''))
            suffix   = f'  —  {role_str}' if role_str else ''
            pdf.mc(6, name_str + suffix)
            for contrib in spk.get('key_contributions', []):
                pdf.bullet(contrib)
            pdf.ln_gap(4)

    # ── Questions Raised ─────────────────────────────────────────────────────
    questions = minutes.get('questionsRaised', [])
    if questions:
        pdf.h1('Questions Raised')
        for q in questions:
            status = (q.get('status') or 'unresolved').lower()
            st_txt, st_bg = _STATUS_COLORS.get(status, ((75, 85, 99), (241, 245, 249)))

            # Question line + status badge
            pdf.set_font('Helvetica', 'B', 10)
            pdf.set_text_color(*pal["text"])
            pdf.set_x(pdf.l_margin)
            q_text = _s(f"Q: {q.get('question', '')}")
            badge_text = status.upper()
            pdf.set_font('Helvetica', 'B', 8)
            badge_w = pdf.get_string_width(badge_text) + 6
            pdf.set_font('Helvetica', 'B', 10)
            avail_w = pdf.epw - badge_w - 4
            pdf.multi_cell(avail_w, 6, q_text)
            # Draw badge next to it (approximate — may overflow on wrap)
            saved_y = pdf.get_y()
            pdf.set_font('Helvetica', 'B', 8)
            pdf.set_fill_color(*st_bg)
            pdf.set_draw_color(*st_txt)
            pdf.set_text_color(*st_txt)
            badge_x = pdf.l_margin + avail_w + 2
            pdf.rect(badge_x, saved_y - 6, badge_w, 5.5, style='FD')
            pdf.set_xy(badge_x + 2, saved_y - 6)
            pdf.cell(badge_w - 4, 5.5, badge_text)
            pdf.set_y(saved_y)

            if q.get('asked_by'):
                pdf.meta_text(f"Asked by {q['asked_by']}", indent=4)
            if q.get('answer_summary'):
                pdf.body(q['answer_summary'], indent=4)
            pdf.ln_gap(4)

    # ── Next Steps ───────────────────────────────────────────────────────────
    next_steps = minutes.get('nextSteps', [])
    if next_steps:
        pdf.h1('Next Steps')
        for step in next_steps:
            pdf.set_font('Helvetica', '', 10)
            pdf.set_text_color(*pal["text"])
            pdf.set_x(pdf.l_margin)
            arrow_w = pdf.get_string_width('->  ')
            pdf.set_font('Helvetica', 'B', 10)
            pdf.set_text_color(*pal["h2"])
            pdf.cell(arrow_w, 6, '->  ')
            pdf.set_font('Helvetica', '', 10)
            pdf.set_text_color(*pal["text"])
            pdf.multi_cell(pdf.epw - arrow_w, 6, _s(step))
        pdf.ln_gap(3)

    pdf.output(output_path)
