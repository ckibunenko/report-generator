import os
import uuid
import json
from datetime import datetime
from flask import Flask, render_template, request, send_file
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas
from PIL import Image
import io

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(os.path.dirname(__file__), 'output')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

# ─── PDF Constants ────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = A4  # 595.28 x 841.89 pt
MARGIN = 38
CONTENT_W = PAGE_W - 2 * MARGIN

# Colors
COLOR_HEADER_BG      = colors.HexColor('#1a2744')
COLOR_BUG_HIGH       = colors.HexColor('#d93025')
COLOR_BUG_MEDIUM     = colors.HexColor('#e8622a')
COLOR_BUG_LOW        = colors.HexColor('#f5a623')
COLOR_SUGGESTION     = colors.HexColor('#4a90d9')
COLOR_LABEL_BG       = colors.HexColor('#f0f0f0')
COLOR_TEXT           = colors.HexColor('#1a1a1a')
COLOR_WHITE          = colors.HexColor('#ffffff')
COLOR_GREEN          = colors.HexColor('#2e7d32')
COLOR_FIXED_BG       = colors.HexColor('#e8f5e9')
COLOR_FIXED_HEADER   = colors.HexColor('#2e7d32')
COLOR_TABLE_ALT      = colors.HexColor('#f8f8f8')
COLOR_SEPARATOR      = colors.HexColor('#cccccc')
COLOR_NARRATIVE      = colors.HexColor('#444444')
COLOR_BADGE_TEXT     = colors.HexColor('#ffffff')
COLOR_STATUS_GREEN   = colors.HexColor('#2e7d32')

SEVERITY_COLORS = {
    'HIGH':       COLOR_BUG_HIGH,
    'MEDIUM':     COLOR_BUG_MEDIUM,
    'LOW':        COLOR_BUG_LOW,
    'SUGGESTION': COLOR_SUGGESTION,
}


def get_severity_color(severity):
    return SEVERITY_COLORS.get(severity.upper(), COLOR_SUGGESTION)


# ─── PageWriter ───────────────────────────────────────────────────────────────
class PageWriter:
    def __init__(self, c, page_w, page_h, margin):
        self.c = c
        self.page_w = page_w
        self.page_h = page_h
        self.margin = margin
        self.content_w = page_w - 2 * margin
        self.y = page_h - margin

    def need(self, height):
        """If not enough space, create new page and reset Y."""
        if self.y - height < self.margin + 40:
            self.c.showPage()
            self.y = self.page_h - self.margin

    def new_page(self):
        self.c.showPage()
        self.y = self.page_h - self.margin

    # ── Cover header ──────────────────────────────────────────────────────────
    def draw_cover_header(self, app_name, app_desc, device):
        header_h = 90
        x = self.margin
        top = self.y

        # Dark blue background
        self.c.setFillColor(COLOR_HEADER_BG)
        self.c.rect(x, top - header_h, self.content_w, header_h, fill=1, stroke=0)

        # Main title
        self.c.setFillColor(COLOR_WHITE)
        self.c.setFont('Helvetica-Bold', 18)
        self.c.drawString(x + 12, top - 28, f'QA Test Report \u2014 {app_name}')

        # Subtitle
        self.c.setFont('Helvetica', 10)
        self.c.drawString(x + 12, top - 46, f'{app_name} \u2014 {app_desc}')

        # Right side: tester / date / device
        date_str = datetime.now().strftime('%d.%m.%Y')
        info_items = [
            f'Tester: Aleksandar Parabucki',
            f'Date: {date_str}',
            f'Device: {device}',
        ]
        self.c.setFont('Helvetica', 8)
        item_w = self.content_w / 3
        self.c.setFillColor(COLOR_WHITE)
        for i, item in enumerate(info_items):
            ix = x + i * item_w
            self.c.drawString(ix + 10, top - header_h + 14, item)

        self.y = top - header_h - 10

    # ── Section title ─────────────────────────────────────────────────────────
    def draw_section_title(self, title, top_pad=16, bot_pad=8):
        self.y -= top_pad
        self.need(30)
        self.c.setFont('Helvetica-Bold', 12)
        self.c.setFillColor(COLOR_TEXT)
        self.c.drawString(self.margin, self.y, title)
        self.y -= bot_pad

    # ── Test Coverage table ───────────────────────────────────────────────────
    def draw_test_coverage_table(self, rows):
        col_w = [self.content_w * 0.35, self.content_w * 0.45, self.content_w * 0.20]
        headers = ['Area', 'Scope', 'Result']
        row_h = 20
        x = self.margin

        # Table header
        self.need(row_h + 4)
        self.c.setFillColor(COLOR_HEADER_BG)
        self.c.rect(x, self.y - row_h, self.content_w, row_h, fill=1, stroke=0)
        self.c.setFillColor(COLOR_WHITE)
        self.c.setFont('Helvetica-Bold', 9)
        cx = x
        for i, h in enumerate(headers):
            self.c.drawString(cx + 6, self.y - row_h + 6, h)
            cx += col_w[i]
        self.y -= row_h

        # Rows
        for idx, row in enumerate(rows):
            self.need(row_h)
            bg = COLOR_TABLE_ALT if idx % 2 == 0 else COLOR_WHITE
            self.c.setFillColor(bg)
            self.c.rect(x, self.y - row_h, self.content_w, row_h, fill=1, stroke=0)
            # light border
            self.c.setStrokeColor(COLOR_SEPARATOR)
            self.c.setLineWidth(0.3)
            self.c.line(x, self.y - row_h, x + self.content_w, self.y - row_h)

            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica', 9)
            cx = x
            values = [row.get('area', ''), row.get('scope', ''), '']
            for i, val in enumerate(values):
                if i == 2:
                    # Green checkmark result
                    self.c.setFillColor(COLOR_STATUS_GREEN)
                    self.c.setFont('Helvetica-Bold', 9)
                    self.c.drawString(cx + 6, self.y - row_h + 6, '\u2713 Tested')
                    self.c.setFillColor(COLOR_TEXT)
                    self.c.setFont('Helvetica', 9)
                else:
                    self.c.drawString(cx + 6, self.y - row_h + 6, str(val))
                cx += col_w[i]
            self.y -= row_h

        # Bottom border
        self.c.setStrokeColor(COLOR_SEPARATOR)
        self.c.setLineWidth(0.5)
        self.c.line(x, self.y, x + self.content_w, self.y)
        self.y -= 4

    # ── Bug Summary table ─────────────────────────────────────────────────────
    def draw_bug_summary_table(self, bugs):
        col_w = [
            self.content_w * 0.06,
            self.content_w * 0.22,
            self.content_w * 0.50,
            self.content_w * 0.22,
        ]
        headers = ['#', 'Type / Severity', 'Issue Area', 'Status']
        row_h = 22
        x = self.margin

        # Table header
        self.need(row_h + 4)
        self.c.setFillColor(COLOR_HEADER_BG)
        self.c.rect(x, self.y - row_h, self.content_w, row_h, fill=1, stroke=0)
        self.c.setFillColor(COLOR_WHITE)
        self.c.setFont('Helvetica-Bold', 9)
        cx = x
        for i, h in enumerate(headers):
            self.c.drawString(cx + 6, self.y - row_h + 7, h)
            cx += col_w[i]
        self.y -= row_h

        for idx, bug in enumerate(bugs):
            is_fixed = bug.get('fixed', False)
            self.need(row_h)

            if is_fixed:
                self.c.setFillColor(COLOR_FIXED_BG)
            else:
                self.c.setFillColor(COLOR_TABLE_ALT if idx % 2 == 0 else COLOR_WHITE)
            self.c.rect(x, self.y - row_h, self.content_w, row_h, fill=1, stroke=0)

            self.c.setStrokeColor(COLOR_SEPARATOR)
            self.c.setLineWidth(0.3)
            self.c.line(x, self.y - row_h, x + self.content_w, self.y - row_h)

            btype = bug.get('type', 'BUG').upper()
            severity = bug.get('severity', '').upper()
            title = bug.get('title', '')
            area = bug.get('area', '')
            build = bug.get('fixed_build', '')

            cx = x
            # # column
            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica-Bold', 9)
            num_str = str(idx + 1)
            if is_fixed:
                self.c.saveState()
                self.c.setStrokeColor(COLOR_TEXT)
                self.c.setLineWidth(0.5)
                sw = self.c.stringWidth(num_str, 'Helvetica-Bold', 9)
                mid_y = self.y - row_h + 7 + 4
                self.c.line(cx + 6, mid_y, cx + 6 + sw, mid_y)
                self.c.restoreState()
            self.c.drawString(cx + 6, self.y - row_h + 7, num_str)
            cx += col_w[0]

            # Type/Severity badge
            badge_label = btype if btype == 'SUGGESTION' else severity
            badge_color = get_severity_color(badge_label if btype == 'SUGGESTION' else severity)
            bw = 70
            bh = 16
            by = self.y - row_h + 3
            self.c.setFillColor(badge_color)
            self.c.roundRect(cx + 4, by, bw, bh, 3, fill=1, stroke=0)
            self.c.setFillColor(COLOR_WHITE)
            self.c.setFont('Helvetica-Bold', 8)
            dot = '\u25cf '
            label_text = f'{dot}{badge_label}' if btype != 'SUGGESTION' else 'SUGGESTION'
            self.c.drawCentredString(cx + 4 + bw / 2, by + 5, label_text)
            cx += col_w[1]

            # Issue Area / Title
            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica', 9)
            area_title = f'{area} \u2014 {title}' if area else title
            # Truncate if too long
            max_chars = int(col_w[2] / 5.2)
            if len(area_title) > max_chars:
                area_title = area_title[:max_chars - 2] + '\u2026'
            if is_fixed:
                self.c.saveState()
                self.c.setStrokeColor(COLOR_TEXT)
                self.c.setLineWidth(0.4)
                sw = self.c.stringWidth(area_title, 'Helvetica', 9)
                mid_y = self.y - row_h + 7 + 4
                self.c.line(cx + 6, mid_y, cx + 6 + sw, mid_y)
                self.c.restoreState()
            self.c.drawString(cx + 6, self.y - row_h + 7, area_title)
            cx += col_w[2]

            # Status
            if is_fixed:
                self.c.setFillColor(COLOR_STATUS_GREEN)
                self.c.setFont('Helvetica-Bold', 9)
                status_text = '\u2713 FIXED'
                if build:
                    status_text += f' {build}'
                self.c.drawString(cx + 6, self.y - row_h + 7, status_text)
            else:
                self.c.drawString(cx + 6, self.y - row_h + 7, '')

            self.y -= row_h

        self.c.setStrokeColor(COLOR_SEPARATOR)
        self.c.setLineWidth(0.5)
        self.c.line(x, self.y, x + self.content_w, self.y)
        self.y -= 4

    # ── Bug detail header bar ─────────────────────────────────────────────────
    def draw_bug_header(self, num, btype, severity, title, is_fixed=False):
        bar_h = 32
        x = self.margin

        bg = COLOR_FIXED_HEADER if is_fixed else COLOR_HEADER_BG
        self.c.setFillColor(bg)
        self.c.rect(x, self.y - bar_h, self.content_w, bar_h, fill=1, stroke=0)

        cur_x = x + 8

        # Left pill: "BUG #N" or "SUGGESTION #N"
        is_suggestion = btype.upper() == 'SUGGESTION'
        if is_suggestion:
            num_label = f'SUGGESTION #{num}'
            pill_color = COLOR_SUGGESTION
        else:
            num_label = f'BUG #{num}'
            pill_color = colors.HexColor('#253a7a')

        self.c.setFillColor(pill_color)
        nw = self.c.stringWidth(num_label, 'Helvetica-Bold', 9) + 12
        # Pill vertically centered in bar: (bar_h - pill_h) / 2 = (32 - 16) / 2 = 8
        self.c.roundRect(cur_x, self.y - bar_h + 8, nw, 16, 3, fill=1, stroke=0)
        self.c.setFillColor(COLOR_WHITE)
        self.c.setFont('Helvetica-Bold', 9)
        self.c.drawString(cur_x + 6, self.y - bar_h + 13, num_label)
        cur_x += nw + 8

        # Severity badge (BUG only, not SUGGESTION)
        if not is_suggestion and severity:
            sev_color = get_severity_color(severity)
            sev_label = f'\u25cf {severity.upper()}'
            sw = self.c.stringWidth(sev_label, 'Helvetica-Bold', 9) + 12
            self.c.setFillColor(sev_color)
            self.c.roundRect(cur_x, self.y - bar_h + 8, sw, 16, 3, fill=1, stroke=0)
            self.c.setFillColor(COLOR_WHITE)
            self.c.setFont('Helvetica-Bold', 9)
            self.c.drawString(cur_x + 6, self.y - bar_h + 13, sev_label)
            cur_x += sw + 10

        # Title
        self.c.setFillColor(COLOR_WHITE)
        self.c.setFont('Helvetica-Bold', 10)
        remaining_w = self.content_w - (cur_x - x) - 8
        title_display = title
        while self.c.stringWidth(title_display, 'Helvetica-Bold', 10) > remaining_w and len(title_display) > 3:
            title_display = title_display[:-1]
        if title_display != title:
            title_display = title_display[:-1] + '\u2026'
        self.c.drawString(cur_x, self.y - bar_h + 12, title_display)

        # Fixed indicator overlay
        if is_fixed:
            self.c.setFillColor(COLOR_WHITE)
            self.c.setFont('Helvetica-Bold', 8)
            fixed_txt = '\u2713 FIXED'
            self.c.drawRightString(x + self.content_w - 8, self.y - bar_h + 13, fixed_txt)

        self.y -= bar_h + 4

    # ── WHAT / WHERE / HOW block ──────────────────────────────────────────────
    def draw_what_where_how(self, what, where, how, is_fixed=False, build_str=''):
        label_w = 55
        text_w = self.content_w - label_w - 4
        font_size = 9
        line_h = 13
        pad = 6

        def block_height(text):
            lines = simpleSplit(text, 'Helvetica', font_size, text_w)
            if not lines:
                lines = ['']
            return max(len(lines), 1) * line_h + pad * 2

        blocks = [('WHAT:', what), ('WHERE:', where), ('HOW:', how)]
        total_h = sum(block_height(t) for _, t in blocks) + 6 * 2

        # Fixed badge extra height
        if is_fixed and build_str:
            total_h += 22

        self.need(total_h)
        x = self.margin
        start_y = self.y

        for label, text in blocks:
            bh = block_height(text)
            lines = simpleSplit(text, 'Helvetica', font_size, text_w)
            if not lines:
                lines = ['']

            # Label background
            self.c.setFillColor(COLOR_LABEL_BG)
            self.c.rect(x, self.y - bh, label_w, bh, fill=1, stroke=0)
            self.c.setStrokeColor(COLOR_SEPARATOR)
            self.c.setLineWidth(0.3)
            self.c.rect(x, self.y - bh, self.content_w, bh, fill=0, stroke=1)

            # Label text
            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica-Bold', font_size)
            self.c.drawString(x + 5, self.y - pad - line_h + 3, label)

            # Body text
            self.c.setFont('Helvetica', font_size)
            ty = self.y - pad
            for line in lines:
                self.c.drawString(x + label_w + 6, ty - line_h + 3, line)
                ty -= line_h

            self.y -= bh + 4

        # Fixed badge in bottom right of block area
        if is_fixed and build_str:
            self.need(22)
            badge_text = f'\u2713 FIXED in build {build_str}'
            bw = self.c.stringWidth(badge_text, 'Helvetica-Bold', 9) + 16
            bx = x + self.content_w - bw - 2
            by = self.y - 18
            self.c.setFillColor(COLOR_STATUS_GREEN)
            self.c.roundRect(bx, by, bw, 16, 3, fill=1, stroke=0)
            self.c.setFillColor(COLOR_WHITE)
            self.c.setFont('Helvetica-Bold', 9)
            self.c.drawString(bx + 8, by + 4, badge_text)
            self.y -= 22

    # ── Narrative description ─────────────────────────────────────────────────
    def draw_description(self, text):
        if not text or not text.strip():
            return
        # Normalize whitespace so pasted text flows as a single paragraph
        text = ' '.join(text.split())
        font_size = 9
        line_h = 13
        lines = simpleSplit(text, 'Helvetica', font_size, self.content_w)
        if not lines:
            return
        total_h = len(lines) * line_h + 8
        self.need(total_h)
        self.y -= 6
        self.c.setFillColor(COLOR_TEXT)
        self.c.setFont('Helvetica', font_size)
        for line in lines:
            self.need(line_h)
            self.c.drawString(self.margin, self.y, line)
            self.y -= line_h
        self.y -= 4

    # ── Fixed verified note ───────────────────────────────────────────────────
    def draw_fixed_note(self, build_str):
        if not build_str:
            return
        self.need(20)
        self.y -= 4
        note = f'\u2713 Verified fixed in build {build_str}'
        self.c.setFont('Helvetica-Oblique', 9)
        self.c.setFillColor(COLOR_STATUS_GREEN)
        self.c.drawString(self.margin, self.y, note)
        self.y -= 14

    # ── Screenshots ───────────────────────────────────────────────────────────
    def draw_screenshots(self, image_paths):
        if not image_paths:
            return

        self.y -= 8

        # Process in pairs
        i = 0
        while i < len(image_paths):
            pair = image_paths[i:i+2]
            if len(pair) == 1:
                self._draw_one_image(pair[0])
            else:
                self._draw_two_images(pair[0], pair[1])
            i += 2

    def _get_image_dims(self, path, max_w):
        """Return (display_w, display_h) preserving aspect ratio."""
        with Image.open(path) as img:
            iw, ih = img.size
        ratio = ih / iw
        dw = min(iw, max_w)
        dh = dw * ratio
        # iPhone portrait cap
        max_h = PAGE_H - 2 * MARGIN - 40
        if dh > max_h:
            dh = max_h
            dw = dh / ratio
        return dw, dh

    def _prepare_image(self, path, crop_aspect=None):
        """Convert image to RGB JPEG. crop_aspect=(w/h) crops to that ratio if given."""
        with Image.open(path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    bg.paste(img, mask=img.split()[-1])
                else:
                    bg.paste(img)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            if crop_aspect is not None:
                iw, ih = img.size
                if iw / ih > crop_aspect:
                    # wider than target: crop sides
                    new_w = int(round(ih * crop_aspect))
                    left = (iw - new_w) // 2
                    img = img.crop((left, 0, left + new_w, ih))
                elif iw / ih < crop_aspect:
                    # taller than target: crop bottom
                    new_h = int(round(iw / crop_aspect))
                    img = img.crop((0, 0, iw, new_h))
            tmp_path = path + '_rgb.jpg'
            img.save(tmp_path, 'JPEG', quality=85)
        return tmp_path

    def _draw_one_image(self, path):
        max_w = int(self.content_w * 0.65)  # ~65% of content width so centering is visible
        try:
            dw, dh = self._get_image_dims(path, max_w)
            self.need(dh + 12)
            tmp = self._prepare_image(path)
            cx = self.margin + (self.content_w - dw) / 2
            self.c.drawImage(tmp, cx, self.y - dh, width=dw, height=dh)
            self.y -= dh + 10
            try:
                os.remove(tmp)
            except Exception:
                pass
        except Exception as e:
            print(f'Image error {path}: {e}')

    def _draw_two_images(self, path1, path2):
        target_w = 230  # display width for each image in pts
        max_h = PAGE_H - 2 * MARGIN - 40
        try:
            with Image.open(path1) as img:
                iw1, ih1 = img.size
            with Image.open(path2) as img:
                iw2, ih2 = img.size
            # Heights if both scaled to target_w wide
            dh1 = target_w * ih1 / iw1
            dh2 = target_w * ih2 / iw2
            # Use the shorter height so both images match; apply page cap
            display_h = min(dh1, dh2, max_h)
            self.need(display_h + 12)
            # Crop each image to the uniform target_w × display_h aspect ratio
            crop_aspect = target_w / display_h
            tmp1 = self._prepare_image(path1, crop_aspect=crop_aspect)
            tmp2 = self._prepare_image(path2, crop_aspect=crop_aspect)
            gap = (self.content_w - target_w * 2) / 3
            x1 = self.margin + gap
            x2 = x1 + target_w + gap
            self.c.drawImage(tmp1, x1, self.y - display_h, width=target_w, height=display_h)
            self.c.drawImage(tmp2, x2, self.y - display_h, width=target_w, height=display_h)
            self.y -= display_h + 10
            for tmp in (tmp1, tmp2):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        except Exception as e:
            print(f'Image pair error: {e}')

    # ── Separator line ────────────────────────────────────────────────────────
    def draw_separator(self):
        self.y -= 12
        self.need(2)
        self.c.setStrokeColor(COLOR_SEPARATOR)
        self.c.setLineWidth(0.8)
        self.c.line(self.margin, self.y, self.margin + self.content_w, self.y)
        self.y -= 12

    # ── Overall Assessment page ───────────────────────────────────────────────
    def draw_overall_assessment(self, text):
        self.need(100)
        if self.y < PAGE_H - MARGIN - 60:
            self.new_page()

        self.draw_section_title('Overall Assessment', top_pad=8, bot_pad=10)

        font_size = 10
        line_h = 15
        if text and text.strip():
            text = ' '.join(text.split())
            lines = simpleSplit(text, 'Helvetica', font_size, self.content_w)
            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica', font_size)
            for line in lines:
                self.need(line_h)
                self.c.drawString(self.margin, self.y, line)
                self.y -= line_h

        self.y -= 20
        self.need(30)
        date_str = datetime.now().strftime('%d.%m.%Y')
        sig = f'Aleksandar Parabucki \u00b7 Senior QA Engineer \u00b7 {date_str} \u00b7 aleksandar.parabucki@gmail.com'
        self.c.setFont('Helvetica', 9)
        self.c.setFillColor(COLOR_NARRATIVE)
        self.c.drawString(self.margin, self.y, sig)
        self.y -= 20

    # ── Page number footer ────────────────────────────────────────────────────
    def draw_page_number(self, page_num):
        self.c.setFont('Helvetica', 8)
        self.c.setFillColor(COLOR_NARRATIVE)
        self.c.drawRightString(
            self.margin + self.content_w,
            self.margin - 18,
            f'Page {page_num}'
        )


# ─── PDF Builder ──────────────────────────────────────────────────────────────
def build_pdf(data, uploaded_files, output_path):
    c = canvas.Canvas(output_path, pagesize=A4)
    pw = PageWriter(c, PAGE_W, PAGE_H, MARGIN)

    app_name = data.get('app_name', 'App')
    app_desc = data.get('app_desc', '')
    device = data.get('device', '')
    coverage_rows = data.get('coverage_rows', [])
    bugs = data.get('bugs', [])
    assessment = data.get('assessment', '')

    page_num = 1

    # ── Page 1: Cover ────────────────────────────────────────────────────────
    pw.draw_cover_header(app_name, app_desc, device)

    pw.draw_section_title('Test Coverage', top_pad=18, bot_pad=6)
    pw.draw_test_coverage_table(coverage_rows)

    if bugs:
        pw.draw_section_title('Bug Summary', top_pad=14, bot_pad=6)
        pw.draw_bug_summary_table(bugs)

    pw.draw_page_number(page_num)

    # ── Pages 2+: Bug Details ─────────────────────────────────────────────────
    for idx, bug in enumerate(bugs):
        btype = bug.get('type', 'BUG').upper()
        severity = bug.get('severity', '').upper()
        title = bug.get('title', '')
        area = bug.get('area', '')
        what = bug.get('what', '')
        where = bug.get('where', '')
        how = bug.get('how', '')
        description = bug.get('description', '')
        is_fixed = bug.get('fixed', False)
        build_str = bug.get('fixed_build', '')
        screenshots = uploaded_files.get(f'bug_{idx}', [])

        # Need space for header + WHAT block (min 80pt)
        pw.need(80)
        if pw.y < PAGE_H - MARGIN - 40:
            pass  # already checked via need()

        # Separator between bugs (not before first)
        if idx > 0:
            pw.draw_separator()

        # Bug header bar
        pw.need(80)
        if pw.y < PAGE_H - MARGIN - 50:
            # Already on a page with content, check if we need a new page
            pass

        pw.draw_bug_header(idx + 1, btype, severity, title, is_fixed=is_fixed)

        # WHAT / WHERE / HOW
        pw.draw_what_where_how(what, where, how, is_fixed=is_fixed, build_str=build_str)

        # Fixed note
        if is_fixed and build_str:
            pw.draw_fixed_note(build_str)

        # Narrative description
        pw.draw_description(description)

        # Screenshots
        if screenshots:
            pw.draw_screenshots(screenshots)

    # ── Last page: Overall Assessment ────────────────────────────────────────
    if assessment and assessment.strip():
        pw.draw_overall_assessment(assessment)
    else:
        # Still draw signature on last page
        if bugs:
            pw.y -= 20
            pw.need(30)
            date_str = datetime.now().strftime('%d.%m.%Y')
            sig = f'Aleksandar Parabucki \u00b7 Senior QA Engineer \u00b7 {date_str} \u00b7 aleksandar.parabucki@gmail.com'
            pw.c.setFont('Helvetica', 9)
            pw.c.setFillColor(COLOR_NARRATIVE)
            pw.c.drawString(MARGIN, pw.y, sig)

    c.save()


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/generate', methods=['POST'])
def generate():
    # Parse form data
    app_name = request.form.get('app_name', 'App').strip()
    app_desc = request.form.get('app_desc', '').strip()
    device = request.form.get('device', '').strip()
    assessment = ' '.join(request.form.get('assessment', '').split())

    # Coverage rows
    coverage_areas = request.form.getlist('coverage_area[]')
    coverage_scopes = request.form.getlist('coverage_scope[]')
    coverage_rows = [
        {'area': a, 'scope': s}
        for a, s in zip(coverage_areas, coverage_scopes)
        if a.strip() or s.strip()
    ]

    # Bugs
    bug_count_str = request.form.get('bug_count', '0')
    try:
        bug_count = int(bug_count_str)
    except ValueError:
        bug_count = 0

    bugs = []
    uploaded_files = {}
    saved_paths = []

    for i in range(bug_count):
        btype = request.form.get(f'bug_type_{i}', 'BUG')
        severity = request.form.get(f'bug_severity_{i}', '')
        title = request.form.get(f'bug_title_{i}', '')
        area = request.form.get(f'bug_area_{i}', '')
        what = ' '.join(request.form.get(f'bug_what_{i}', '').split())
        where = ' '.join(request.form.get(f'bug_where_{i}', '').split())
        how = ' '.join(request.form.get(f'bug_how_{i}', '').split())
        description = ' '.join(request.form.get(f'bug_description_{i}', '').split())
        fixed = request.form.get(f'bug_fixed_{i}', 'false') == 'true'
        fixed_build = request.form.get(f'bug_fixed_build_{i}', '')

        bugs.append({
            'type': btype,
            'severity': severity,
            'title': title,
            'area': area,
            'what': what,
            'where': where,
            'how': how,
            'description': description,
            'fixed': fixed,
            'fixed_build': fixed_build,
        })

        # Handle screenshots for this bug
        file_key = f'bug_screenshots_{i}'
        files = request.files.getlist(file_key)
        bug_images = []
        for f in files:
            if f and f.filename:
                ext = os.path.splitext(f.filename)[1].lower() or '.jpg'
                fname = f'{uuid.uuid4().hex}{ext}'
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                f.save(fpath)
                saved_paths.append(fpath)
                bug_images.append(fpath)
        if bug_images:
            uploaded_files[f'bug_{i}'] = bug_images

    data = {
        'app_name': app_name,
        'app_desc': app_desc,
        'device': device,
        'coverage_rows': coverage_rows,
        'bugs': bugs,
        'assessment': assessment,
    }

    # Generate PDF
    out_name = f'QA_Report_{app_name.replace(" ", "_")}_{uuid.uuid4().hex[:6]}.pdf'
    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_name)

    try:
        build_pdf(data, uploaded_files, out_path)
    finally:
        for p in saved_paths:
            try:
                os.remove(p)
            except Exception:
                pass

    return send_file(out_path, as_attachment=True, download_name=out_name, mimetype='application/pdf')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5001)
    args, _ = parser.parse_known_args()
    app.run(debug=True, port=args.port)
