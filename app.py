import os
import re
import uuid
import json
from datetime import datetime
from flask import Flask, render_template, request, send_file, jsonify, abort
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

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _smart_truncate(text, max_len=51):
    """Truncate at a word boundary, appending … when shortened."""
    if len(text) <= max_len:
        return text
    # Leave one char for the ellipsis
    candidate = text[:max_len - 1]
    last_space = candidate.rfind(' ')
    if last_space > 0:
        candidate = candidate[:last_space]
    return candidate + '\u2026'


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


def clean_text(text):
    """Replace all newline variants with a single space and collapse whitespace."""
    text = text.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
    return ' '.join(text.split())


# ─── PageWriter ───────────────────────────────────────────────────────────────
class PageWriter:
    def __init__(self, c, page_w, page_h, margin):
        self.c = c
        self.page_w = page_w
        self.page_h = page_h
        self.margin = margin
        self.content_w = page_w - 2 * margin
        self.y = page_h - margin
        self.page_num = 1

    def _draw_footer(self, is_last=False):
        self.c.setFont('Helvetica', 8)
        self.c.setFillColor(COLOR_NARRATIVE)
        self.c.drawRightString(
            self.margin + self.content_w,
            self.margin - 18,
            f'Page {self.page_num}'
        )
        if is_last:
            sig = 'Aleksandar Parabucki \u00b7 Senior QA Engineer \u00b7 aleksandar.parabucki@gmail.com'
            self.c.drawString(self.margin, self.margin - 18, sig)

    def finalize(self):
        """Draw footer on the last page without calling showPage."""
        self._draw_footer(is_last=True)

    def need(self, height):
        """If not enough space, create new page and reset Y."""
        if self.y - height < self.margin + 40:
            self._draw_footer()
            self.c.showPage()
            self.page_num += 1
            self.y = self.page_h - self.margin

    def new_page(self):
        self._draw_footer()
        self.c.showPage()
        self.page_num += 1
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
        subtitle = f'{app_name} \u2014 {app_desc}' if app_desc else app_name
        self.c.drawString(x + 12, top - 46, subtitle)

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

            # Type/Severity badge — same style as header bar: 9pt font, dynamic width
            badge_label = btype if btype == 'SUGGESTION' else severity
            badge_color = get_severity_color(badge_label if btype == 'SUGGESTION' else severity)
            dot = '\u25cf '
            label_text = f'{dot}{badge_label}' if btype != 'SUGGESTION' else 'SUGGESTION'
            bh = 16
            by = self.y - row_h + 3
            self.c.setFont('Helvetica-Bold', 9)
            bw = self.c.stringWidth(label_text, 'Helvetica-Bold', 9) + 12
            self.c.setFillColor(badge_color)
            self.c.roundRect(cx + 4, by, bw, bh, 3, fill=1, stroke=0)
            self.c.setFillColor(COLOR_WHITE)
            self.c.drawString(cx + 4 + 6, by + 5, label_text)
            cx += col_w[1]

            # Issue Area / Title
            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica', 9)
            area_title = f'{area} \u2014 {title}' if area else title
            area_title = _smart_truncate(area_title)
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
    def _wwh_height(self, what, where, how, is_fixed=False, build_str=''):
        label_w = 55
        text_x_offset = 6
        text_w = self.content_w - label_w - text_x_offset - 4
        font_size = 9
        line_h = 13
        pad = 6
        total = 6 * 2  # outer padding
        for text in (what, where, how):
            lines = simpleSplit(text, 'Helvetica', font_size, text_w)
            if not lines:
                lines = ['']
            total += max(len(lines), 1) * line_h + pad * 2 + 4
        if is_fixed and build_str:
            total += 22
        return total

    def draw_what_where_how(self, what, where, how, is_fixed=False, build_str=''):
        label_w = 55
        text_x_offset = 6          # pixels from label_w to text start
        text_w = self.content_w - label_w - text_x_offset - 4  # available from draw pos to right margin
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
                self.c.drawString(x + label_w + text_x_offset, ty - line_h + 3, line)
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
        font_size = 9
        line_h = 13
        lines = simpleSplit(text, 'Helvetica', font_size, self.content_w)
        if not lines:
            return
        total_h = len(lines) * line_h + 6
        self.need(total_h)
        self.y -= 4
        self.c.setFillColor(COLOR_TEXT)
        self.c.setFont('Helvetica', font_size)
        for line in lines:
            self.need(line_h)
            self.c.drawString(self.margin, self.y, line)
            self.y -= line_h
        self.y -= 2

    # ── Fixed verified note ───────────────────────────────────────────────────
    def draw_fixed_note(self, build_str):
        if not build_str:
            return
        self.need(16)
        self.y -= 2
        note = f'\u2713 Verified fixed in build {build_str}'
        self.c.setFont('Helvetica-Oblique', 9)
        self.c.setFillColor(COLOR_STATUS_GREEN)
        self.c.drawString(self.margin, self.y, note)
        self.y -= 8

    # ── Screenshots ───────────────────────────────────────────────────────────
    def draw_screenshots(self, image_paths):
        if not image_paths:
            return

        self.y -= 4

        def _is_portrait(path):
            try:
                with Image.open(path) as img:
                    iw, ih = img.size
                return ih > iw
            except Exception:
                return True  # treat unknown as portrait

        i = 0
        while i < len(image_paths):
            path1 = image_paths[i]
            # Only pair two consecutive portraits side-by-side;
            # landscape/square images always display full-width one at a time
            if (_is_portrait(path1)
                    and i + 1 < len(image_paths)
                    and _is_portrait(image_paths[i + 1])):
                self._draw_two_images(path1, image_paths[i + 1])
                i += 2
            else:
                self._draw_one_image(path1)
                i += 1

    @staticmethod
    def _img_dims(iw, ih, max_w):
        """Scale to max_w, never up, preserve aspect ratio, cap at page height."""
        dw = min(iw, max_w)
        dh = dw * ih / iw
        max_h = PAGE_H - 2 * MARGIN - 40
        if dh > max_h:
            dh = max_h
            dw = dh * iw / ih
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
        try:
            with Image.open(path) as img:
                iw, ih = img.size
            max_w = 240 if ih > iw else 400
            dw, dh = self._img_dims(iw, ih, max_w)
            self.need(dh + 12)
            tmp = self._prepare_image(path)
            cx = self.margin + (self.content_w - dw) / 2
            self.c.drawImage(tmp, cx, self.y - dh, width=dw, height=dh)
            self.y -= dh + 6
            try:
                os.remove(tmp)
            except Exception:
                pass
        except Exception as e:
            print(f'Image error {path}: {e}')

    def _draw_two_images(self, path1, path2):
        MAX_W = 210
        GAP = 10
        try:
            with Image.open(path1) as img:
                iw1, ih1 = img.size
            with Image.open(path2) as img:
                iw2, ih2 = img.size
            dw1, dh1 = self._img_dims(iw1, ih1, MAX_W)
            dw2, dh2 = self._img_dims(iw2, ih2, MAX_W)
            display_h = max(dh1, dh2)
            self.need(display_h + 12)
            tmp1 = self._prepare_image(path1)
            tmp2 = self._prepare_image(path2)
            total_w = dw1 + GAP + dw2
            x1 = self.margin + (self.content_w - total_w) / 2
            x2 = x1 + dw1 + GAP
            self.c.drawImage(tmp1, x1, self.y - dh1, width=dw1, height=dh1)
            self.c.drawImage(tmp2, x2, self.y - dh2, width=dw2, height=dh2)
            self.y -= display_h + 6
            for tmp in (tmp1, tmp2):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        except Exception as e:
            print(f'Image pair error: {e}')

    def _first_image_block_height(self, paths):
        """Return display height of the first image block (single or portrait pair)."""
        if not paths:
            return 0

        def _is_portrait(p):
            try:
                with Image.open(p) as img:
                    iw, ih = img.size
                return ih > iw
            except Exception:
                return True

        def _dims_from_path(p, max_w):
            try:
                with Image.open(p) as img:
                    iw, ih = img.size
                return self._img_dims(iw, ih, max_w)
            except Exception:
                return max_w, PAGE_H - 2 * MARGIN - 40

        path1 = paths[0]
        if _is_portrait(path1) and len(paths) >= 2 and _is_portrait(paths[1]):
            _, dh1 = _dims_from_path(path1, 210)
            _, dh2 = _dims_from_path(paths[1], 210)
            return max(dh1, dh2)
        else:
            max_w = 240 if _is_portrait(path1) else 400
            _, dh = _dims_from_path(path1, max_w)
            return dh

    # ── Separator line ────────────────────────────────────────────────────────
    def draw_separator(self):
        self.y -= 5
        self.need(2)
        self.c.setStrokeColor(COLOR_SEPARATOR)
        self.c.setLineWidth(0.8)
        self.c.line(self.margin, self.y, self.margin + self.content_w, self.y)
        self.y -= 9

    # ── Overall Assessment page ───────────────────────────────────────────────
    def draw_overall_assessment(self, text):
        # Always begin on a fresh page — prevents heading being orphaned from content
        self.new_page()
        self.draw_section_title('Overall Assessment', top_pad=8, bot_pad=10)

        font_size = 10
        line_h = 15
        if text and text.strip():
            text = clean_text(text)
            lines = simpleSplit(text, 'Helvetica', font_size, self.content_w)
            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica', font_size)
            for line in lines:
                self.need(line_h)
                self.c.drawString(self.margin, self.y, line)
                self.y -= line_h


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

    # ── Page 1: Cover ────────────────────────────────────────────────────────
    pw.draw_cover_header(app_name, app_desc, device)

    pw.draw_section_title('Test Coverage', top_pad=18, bot_pad=6)
    pw.draw_test_coverage_table(coverage_rows)

    if bugs:
        pw.draw_section_title('Bug Summary', top_pad=14, bot_pad=6)
        pw.draw_bug_summary_table(bugs)

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

        # Compute total height needed: header + WWH + description + fixed note + first image
        header_h = 36
        sep_h = 14
        wwh_h = pw._wwh_height(what, where, how, is_fixed=is_fixed, build_str=build_str)
        desc_lines = simpleSplit(description, 'Helvetica', 9, pw.content_w) if description else []
        desc_h = (len(desc_lines) * 13 + 6) if desc_lines else 0
        fixed_h = 10 if (is_fixed and build_str) else 0
        first_img_h = (4 + pw._first_image_block_height(screenshots)) if screenshots else 0
        total_need = header_h + wwh_h + desc_h + fixed_h + first_img_h

        if idx == 0:
            pw.need(total_need)
        else:
            if pw.y - (sep_h + total_need) < MARGIN + 40:
                pw.new_page()
            else:
                pw.draw_separator()

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
    pw.draw_overall_assessment(assessment)

    pw.finalize()
    c.save()


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/generate', methods=['POST'])
def generate():
    # Parse form data
    app_name = request.form.get('app_name', 'App').strip()
    # Strip accidental "QA Test Report — " prefix if user typed the full title
    app_name = re.sub(r'^QA\s+Test\s+Report\s*[\u2014\-]\s*', '', app_name).strip() or 'App'
    app_desc = request.form.get('app_desc', '').strip()
    device = request.form.get('device', '').strip()
    assessment = clean_text(request.form.get('assessment', ''))

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
        what = clean_text(request.form.get(f'bug_what_{i}', ''))
        where = clean_text(request.form.get(f'bug_where_{i}', ''))
        how = clean_text(request.form.get(f'bug_how_{i}', ''))
        description = clean_text(request.form.get(f'bug_description_{i}', ''))
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

    # Generate PDF + JSON
    slug = f'{app_name.replace(" ", "_")}_{uuid.uuid4().hex[:6]}'
    out_name = f'QA_Report_{slug}.pdf'
    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_name)
    json_name = f'QA_Report_{slug}.json'
    json_path = os.path.join(app.config['OUTPUT_FOLDER'], json_name)

    try:
        build_pdf(data, uploaded_files, out_path)
    finally:
        for p in saved_paths:
            try:
                os.remove(p)
            except Exception:
                pass

    report_json = {
        'app_name': app_name,
        'app_desc': app_desc,
        'device': device,
        'assessment': assessment,
        'coverage': coverage_rows,
        'bugs': [
            {
                'type': b['type'],
                'severity': b['severity'],
                'title': b['title'],
                'area': b['area'],
                'what': b['what'],
                'where': b['where'],
                'how': b['how'],
                'description': b['description'],
                'fixed': b['fixed'],
                'fixed_build': b['fixed_build'],
            }
            for b in bugs
        ],
    }
    with open(json_path, 'w', encoding='utf-8') as fh:
        json.dump(report_json, fh, ensure_ascii=False, indent=2)

    return jsonify({'pdf_url': f'/download/{out_name}', 'json_url': f'/download/{json_name}',
                    'pdf_name': out_name, 'json_name': json_name})


@app.route('/download/<filename>')
def download_file(filename):
    if '/' in filename or '\\' in filename or '..' in filename:
        abort(400)
    fpath = os.path.join(app.config['OUTPUT_FOLDER'], filename)
    if not os.path.isfile(fpath):
        abort(404)
    return send_file(fpath, as_attachment=True, download_name=filename)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5001)
    args, _ = parser.parse_known_args()
    app.run(debug=True, port=args.port)
