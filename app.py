import os
import re
import uuid
import json
import time
from datetime import datetime
from flask import Flask, render_template, request, send_file, jsonify, abort
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas
from PIL import Image, ImageOps
import io

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(os.path.dirname(__file__), 'output')
# Internal tool: allow larger raw uploads because screenshots are optimized after receipt.
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.config['REPORT_PREFIX'] = 'QA_Report_'
app.config['REPORT_TTL_SECONDS'] = 24 * 60 * 60
app.config['SCREENSHOT_TARGET_MIN_BYTES'] = 100 * 1024
app.config['SCREENSHOT_TARGET_MAX_BYTES'] = 300 * 1024
app.config['SCREENSHOT_KEEP_ORIGINAL_MAX_BYTES'] = 300 * 1024

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

ALLOWED_IMAGE_FORMATS = {
    'PNG': '.png',
    'JPEG': '.jpg',
    'WEBP': '.webp',
}

SCREENSHOT_WEBP_QUALITIES = [90, 86, 82, 78, 74, 70, 66, 62]
SCREENSHOT_RESIZE_SCALES = [0.92, 0.85, 0.78, 0.7, 0.62, 0.55]
SCREENSHOT_RESIZE_QUALITIES = [80, 74, 68, 62, 56, 52]
RESAMPLING_LANCZOS = getattr(Image, 'Resampling', Image).LANCZOS

# ─── PDF Constants ────────────────────────────────────────────────────────────
PAGE_W, PAGE_H = A4  # 595.28 x 841.89 pt
MARGIN = 38
CONTENT_W = PAGE_W - 2 * MARGIN

# Colors
COLOR_HEADER_BG      = colors.HexColor('#1b2742')
COLOR_HEADER_BG_SOFT = colors.HexColor('#2a3a5d')
COLOR_BUG_HIGH       = colors.HexColor('#c74b3b')
COLOR_BUG_MEDIUM     = colors.HexColor('#dc8b35')
COLOR_BUG_LOW        = colors.HexColor('#d9ae4c')
COLOR_SUGGESTION     = colors.HexColor('#5f86c5')
COLOR_ACCENT         = colors.HexColor('#d39a55')
COLOR_ACCENT_SOFT    = colors.HexColor('#f3e3cf')
COLOR_PAGE_BG        = colors.HexColor('#f7f2e9')
COLOR_SURFACE        = colors.HexColor('#f7f3ec')
COLOR_SURFACE_ALT    = colors.HexColor('#fbf8f2')
COLOR_LABEL_BG       = colors.HexColor('#efe8dc')
COLOR_LABEL_WHAT     = colors.HexColor('#f4e2cd')
COLOR_LABEL_WHERE    = colors.HexColor('#e4ebf5')
COLOR_LABEL_HOW      = colors.HexColor('#e1efe7')
COLOR_TEXT           = colors.HexColor('#1b2333')
COLOR_TEXT_MUTED     = colors.HexColor('#697286')
COLOR_WHITE          = colors.HexColor('#ffffff')
COLOR_GREEN          = colors.HexColor('#2e7d32')
COLOR_FIXED_BG       = colors.HexColor('#e8f2eb')
COLOR_FIXED_HEADER   = colors.HexColor('#2f6a4f')
COLOR_TABLE_ALT      = colors.HexColor('#faf6ef')
COLOR_SEPARATOR      = colors.HexColor('#d7cfc2')
COLOR_NARRATIVE      = colors.HexColor('#4d5769')
COLOR_BADGE_TEXT     = colors.HexColor('#ffffff')
COLOR_STATUS_GREEN   = colors.HexColor('#2f7a4c')
COLOR_PANEL_BORDER   = colors.HexColor('#d8cfc2')
COLOR_IMAGE_FRAME    = colors.HexColor('#efe6d7')

WWH_LABEL_COLORS = {
    'WHAT:': COLOR_LABEL_WHAT,
    'WHERE:': COLOR_LABEL_WHERE,
    'HOW:': COLOR_LABEL_HOW,
}

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


def build_report_slug(app_name):
    """Create a filesystem-safe slug without changing the report title shown in the PDF."""
    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', app_name.strip())
    safe_name = re.sub(r'_+', '_', safe_name).strip('._-')
    return safe_name[:80] or 'App'


def cleanup_paths(paths):
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            app.logger.warning('Failed to remove temporary file: %s', path)


def cleanup_old_reports():
    cutoff = time.time() - app.config['REPORT_TTL_SECONDS']
    prefix = app.config['REPORT_PREFIX']

    for entry in os.listdir(app.config['OUTPUT_FOLDER']):
        if not entry.startswith(prefix):
            continue

        path = os.path.join(app.config['OUTPUT_FOLDER'], entry)
        if not os.path.isfile(path):
            continue

        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            app.logger.warning('Failed to clean stale report: %s', path)


def validate_image_upload(file_storage):
    filename = os.path.basename(file_storage.filename or 'upload')

    try:
        with Image.open(file_storage.stream) as img:
            img.verify()
            image_format = (img.format or '').upper()
    except Exception as exc:
        raise ValueError(
            f'"{filename}" is not a valid image. Please upload PNG, JPG, or WEBP screenshots.'
        ) from exc
    finally:
        file_storage.stream.seek(0)

    return ALLOWED_IMAGE_FORMATS.get(image_format, '.webp')


def _flatten_image_to_rgb(img):
    if img.mode in ('RGBA', 'LA'):
        base = Image.new('RGB', img.size, (255, 255, 255))
        base.paste(img, mask=img.getchannel('A'))
        return base

    if img.mode == 'P':
        if 'transparency' in img.info:
            rgba = img.convert('RGBA')
            base = Image.new('RGB', rgba.size, (255, 255, 255))
            base.paste(rgba, mask=rgba.getchannel('A'))
            return base
        return img.convert('RGB')

    if img.mode != 'RGB':
        return img.convert('RGB')

    return img.copy()


def _normalize_image_orientation(img):
    return ImageOps.exif_transpose(img)


def _encode_image_bytes(img, image_format, **save_kwargs):
    buffer = io.BytesIO()
    img.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue()


def _resize_image(img, scale):
    width, height = img.size
    resized = (
        max(1, int(round(width * scale))),
        max(1, int(round(height * scale))),
    )
    if resized == img.size:
        return img.copy()
    return img.resize(resized, RESAMPLING_LANCZOS)


def optimize_uploaded_image(file_storage, original_ext):
    original_bytes = file_storage.stream.read()
    file_storage.stream.seek(0)

    target_max = app.config['SCREENSHOT_TARGET_MAX_BYTES']
    best_bytes = original_bytes
    best_ext = original_ext

    with Image.open(io.BytesIO(original_bytes)) as source_img:
        source_format = (source_img.format or '').upper()
        source_img = _normalize_image_orientation(source_img)
        source_img.load()
        base_img = _flatten_image_to_rgb(source_img)

    if (
        source_format in ALLOWED_IMAGE_FORMATS
        and len(original_bytes) <= app.config['SCREENSHOT_KEEP_ORIGINAL_MAX_BYTES']
    ):
        return original_bytes, original_ext

    def consider_candidate(img, quality):
        nonlocal best_bytes, best_ext
        try:
            encoded = _encode_image_bytes(
                img,
                'WEBP',
                quality=quality,
                method=6,
            )
            if len(encoded) < len(best_bytes):
                best_bytes = encoded
                best_ext = '.webp'
            if len(encoded) <= target_max:
                return encoded, '.webp'
        except Exception:
            return None
        return None

    for quality in SCREENSHOT_WEBP_QUALITIES:
        result = consider_candidate(base_img, quality)
        if result:
            return result

    for scale in SCREENSHOT_RESIZE_SCALES:
        resized = _resize_image(base_img, scale)
        for quality in SCREENSHOT_RESIZE_QUALITIES:
            result = consider_candidate(resized, quality)
            if result:
                return result

    return best_bytes, best_ext


def format_upload_limit():
    limit = app.config['MAX_CONTENT_LENGTH']
    if limit % (1024 * 1024) == 0:
        return f'{limit // (1024 * 1024)} MB'
    if limit % 1024 == 0:
        return f'{limit // 1024} KB'
    return f'{limit} bytes'


def parse_iso_date(value, field_label):
    try:
        return datetime.strptime(value, '%Y-%m-%d')
    except ValueError as exc:
        raise ValueError(f'{field_label} must use YYYY-MM-DD format.') from exc


def build_report_date_context(report_date_mode, report_date, report_start_date, report_end_date):
    mode = report_date_mode if report_date_mode in {'single', 'range'} else 'single'

    if mode == 'range':
        if not report_start_date or not report_end_date:
            raise ValueError('Please select both start and end dates for a date range report.')

        start_dt = parse_iso_date(report_start_date, 'Start date')
        end_dt = parse_iso_date(report_end_date, 'End date')
        if end_dt < start_dt:
            raise ValueError('End date must be the same as or later than start date.')

        if start_dt.date() == end_dt.date():
            label = start_dt.strftime('%d.%m.%Y')
        else:
            label = f'{start_dt.strftime("%d.%m.%Y")} - {end_dt.strftime("%d.%m.%Y")}'

        return {
            'mode': 'range',
            'date': '',
            'start_date': report_start_date,
            'end_date': report_end_date,
            'label': label,
        }

    normalized_date = report_date or datetime.now().strftime('%Y-%m-%d')
    report_dt = parse_iso_date(normalized_date, 'Report date')
    return {
        'mode': 'single',
        'date': normalized_date,
        'start_date': '',
        'end_date': '',
        'label': report_dt.strftime('%d.%m.%Y'),
    }


def wrap_pdf_text(text, font_name, font_size, max_width, max_lines=None):
    lines = simpleSplit(text, font_name, font_size, max_width)
    if not max_lines or len(lines) <= max_lines:
        return lines

    truncated = lines[:max_lines]
    last_line = truncated[-1].rstrip()
    ellipsis = '\u2026'

    while last_line and pdfmetrics.stringWidth(last_line + ellipsis, font_name, font_size) > max_width:
        last_line = last_line[:-1].rstrip()

    truncated[-1] = (last_line + ellipsis) if last_line else ellipsis
    return truncated


def build_cover_subtitle(app_name, app_desc):
    app_name_norm = ' '.join((app_name or '').lower().split())
    app_desc_norm = ' '.join((app_desc or '').lower().split())

    if app_desc and app_desc_norm and app_desc_norm not in app_name_norm:
        return app_desc
    return ''


def bug_sort_key(entry):
    bug = entry['bug']
    btype = (bug.get('type') or 'BUG').upper()
    severity = (bug.get('severity') or '').upper()

    if btype == 'SUGGESTION':
        return (1, 0, entry['original_index'])

    severity_rank = {
        'HIGH': 0,
        'MEDIUM': 1,
        'LOW': 2,
    }.get(severity, 3)
    return (0, severity_rank, entry['original_index'])


def sort_bug_entries_for_pdf(bugs, uploaded_files):
    entries = [
        {
            'bug': bug,
            'screenshots': uploaded_files.get(f'bug_{idx}', []),
            'original_index': idx,
        }
        for idx, bug in enumerate(bugs)
    ]
    return sorted(entries, key=bug_sort_key)


def estimate_bug_start_height(pw, bug):
    what = bug.get('what', '')
    header_h = pw._bug_header_layout(
        bug.get('type', 'BUG'),
        bug.get('severity', ''),
        bug.get('title', ''),
        bug.get('area', ''),
        bug.get('fixed', False),
    )['height']
    what_h = pw._wwh_block_height(what)
    return header_h + what_h + 4


# ─── PageWriter ───────────────────────────────────────────────────────────────
class PageWriter:
    MIN_INLINE_IMAGE_HEIGHT = 220
    MIN_INLINE_PAIR_HEIGHT = 180
    MIN_TRAILING_IMAGE_HEIGHT = 140
    MIN_TRAILING_PAIR_HEIGHT = 120
    IMAGE_CARD_PAD = 10

    def __init__(self, c, page_w, page_h, margin):
        self.c = c
        self.page_w = page_w
        self.page_h = page_h
        self.margin = margin
        self.content_w = page_w - 2 * margin
        self.y = page_h - margin
        self.page_num = 1
        self._paint_page_background()

    def _paint_page_background(self):
        self.c.setFillColor(COLOR_PAGE_BG)
        self.c.rect(0, 0, self.page_w, self.page_h, fill=1, stroke=0)
        self.c.setFillColor(COLOR_WHITE)
        self.c.roundRect(
            self.margin - 8,
            self.margin - 4,
            self.content_w + 16,
            self.page_h - (self.margin * 2) + 18,
            14,
            fill=1,
            stroke=0,
        )

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
            self._paint_page_background()
            self.y = self.page_h - self.margin

    def new_page(self):
        self._draw_footer()
        self.c.showPage()
        self.page_num += 1
        self._paint_page_background()
        self.y = self.page_h - self.margin

    def _draw_pill(self, x, y, text, bg_color, text_color=COLOR_WHITE, font_size=8, pad_x=7, height=16):
        width = self.c.stringWidth(text, 'Helvetica-Bold', font_size) + pad_x * 2
        self.c.setFillColor(bg_color)
        self.c.roundRect(x, y, width, height, 4, fill=1, stroke=0)
        self.c.setFillColor(text_color)
        self.c.setFont('Helvetica-Bold', font_size)
        self.c.drawString(x + pad_x, y + (height - font_size) / 2 + 1, text)
        return width

    def _draw_info_card(self, x, y, width, height, label, value):
        self.c.setFillColor(COLOR_HEADER_BG_SOFT)
        self.c.roundRect(x, y, width, height, 8, fill=1, stroke=0)
        self.c.setFillColor(COLOR_ACCENT_SOFT)
        self.c.setFont('Helvetica-Bold', 6.5)
        self.c.drawString(x + 8, y + height - 12, label.upper())
        value_lines = wrap_pdf_text(str(value), 'Helvetica-Bold', 7.8, width - 16, max_lines=2)
        self.c.setFillColor(COLOR_WHITE)
        self.c.setFont('Helvetica-Bold', 7.8)
        line_y = y + height - 26
        for line in value_lines:
            self.c.drawString(x + 8, line_y, line)
            line_y -= 10

    def _draw_image_frame(self, x, y, width, height):
        pad = self.IMAGE_CARD_PAD
        self.c.setFillColor(COLOR_IMAGE_FRAME)
        self.c.roundRect(x - pad, y - pad, width + pad * 2, height + pad * 2, 10, fill=1, stroke=0)
        self.c.setFillColor(COLOR_WHITE)
        self.c.roundRect(x - pad + 2, y - pad + 2, width + (pad - 2) * 2, height + (pad - 2) * 2, 9, fill=1, stroke=0)

    def _bug_header_layout(self, btype, severity, title, area, is_fixed=False):
        x = self.margin
        is_suggestion = str(btype).upper() == 'SUGGESTION'
        num_label = 'SUGGESTION' if is_suggestion else 'BUG'
        pill_color = COLOR_SUGGESTION if is_suggestion else COLOR_HEADER_BG_SOFT
        sev_label = f'\u25cf {str(severity).upper()}' if (not is_suggestion and severity) else ''
        cur_x = x + 14
        first_pill_w = self.c.stringWidth(f'{num_label} #88', 'Helvetica-Bold', 8) + 14
        cur_x += first_pill_w + 8
        if sev_label:
            sev_w = self.c.stringWidth(sev_label, 'Helvetica-Bold', 8) + 14
            cur_x += sev_w + 10
        fixed_w = self.c.stringWidth('\u2713 FIXED', 'Helvetica-Bold', 8) + 24 if is_fixed else 0
        remaining_w = self.content_w - (cur_x - x) - fixed_w - 14
        title_lines = wrap_pdf_text(title or 'Untitled finding', 'Helvetica-Bold', 10.6, remaining_w, max_lines=2)
        bar_h = 38 + max(0, len(title_lines) - 1) * 12
        return {
            'height': max(42, bar_h),
            'x': cur_x,
            'remaining_w': remaining_w,
            'is_suggestion': is_suggestion,
            'num_label': num_label,
            'pill_color': pill_color,
            'sev_label': sev_label,
            'title_lines': title_lines,
        }

    # ── Cover header ──────────────────────────────────────────────────────────
    def draw_cover_header(self, app_name, app_desc, device, report_date_label):
        title_lines = wrap_pdf_text(app_name, 'Helvetica-Bold', 18, self.content_w - 34, max_lines=2)
        subtitle = build_cover_subtitle(app_name, app_desc)
        subtitle_lines = wrap_pdf_text(subtitle, 'Helvetica', 9.5, self.content_w - 34, max_lines=2) if subtitle else []

        card_h = 38
        top_pad = 18
        eyebrow_gap = 16
        title_gap = 18
        subtitle_gap = 14 if subtitle_lines else 0
        title_block_h = len(title_lines) * 20
        subtitle_block_h = len(subtitle_lines) * 11
        header_h = top_pad + eyebrow_gap + title_block_h + title_gap + subtitle_block_h + subtitle_gap + card_h + 18
        x = self.margin
        top = self.y

        self.c.setFillColor(COLOR_ACCENT)
        self.c.roundRect(x, top - header_h, self.content_w, header_h, 12, fill=1, stroke=0)

        self.c.setFillColor(COLOR_HEADER_BG)
        self.c.roundRect(x, top - header_h + 4, self.content_w, header_h - 4, 12, fill=1, stroke=0)

        self.c.setFillColor(COLOR_ACCENT)
        self.c.roundRect(x + 16, top - 18, 56, 6, 3, fill=1, stroke=0)
        self.c.setFillColor(COLOR_ACCENT_SOFT)
        self.c.setFont('Helvetica-Bold', 7)
        self.c.drawString(x + 16, top - 32, 'QA REPORT')

        # Main title
        self.c.setFillColor(COLOR_WHITE)
        self.c.setFont('Helvetica-Bold', 18)
        title_y = top - 58
        for idx, line in enumerate(title_lines):
            baseline = title_y - idx * 20
            if idx == 0:
                self.c.drawString(x + 16, baseline, line)
            else:
                self.c.drawCentredString(x + self.content_w / 2, baseline, line)

        # Subtitle
        if subtitle_lines:
            self.c.setFont('Helvetica', 9.5)
            self.c.setFillColor(COLOR_ACCENT_SOFT)
            subtitle_y = title_y - title_block_h - 8
            for idx, line in enumerate(subtitle_lines):
                self.c.drawString(x + 16, subtitle_y - idx * 11, line)

        # Bottom info cards
        chip_inset = 10
        chip_y = top - header_h + 18
        gap = 12
        chip_total_w = self.content_w - chip_inset * 2
        chip_w = (chip_total_w - gap * 2) / 3
        cards = [
            ('Tester', 'Aleksandar Parabucki'),
            ('Date', report_date_label),
            ('Device', device or 'Not specified'),
        ]
        for idx, (label, value) in enumerate(cards):
            self._draw_info_card(x + chip_inset + idx * (chip_w + gap), chip_y, chip_w, card_h, label, value)

        self.y = top - header_h - 12

    # ── Section title ─────────────────────────────────────────────────────────
    def draw_section_title(self, title, top_pad=16, bot_pad=8):
        self.y -= top_pad
        self.need(40)
        self.c.setFillColor(COLOR_ACCENT)
        self.c.roundRect(self.margin, self.y - 2, 24, 5, 2.5, fill=1, stroke=0)
        self.c.setFont('Helvetica-Bold', 13.5)
        self.c.setFillColor(COLOR_TEXT)
        title_x = self.margin + 34
        self.c.drawString(title_x, self.y, title)
        title_w = self.c.stringWidth(title, 'Helvetica-Bold', 13.5)
        rule_y = self.y - 2
        self.c.setStrokeColor(COLOR_SEPARATOR)
        self.c.setLineWidth(0.8)
        self.c.line(title_x + title_w + 14, rule_y, self.margin + self.content_w, rule_y)
        self.y -= bot_pad

    # ── Test Coverage table ───────────────────────────────────────────────────
    def draw_test_coverage_table(self, rows):
        col_w = [self.content_w * 0.35, self.content_w * 0.45, self.content_w * 0.20]
        headers = ['Area', 'Scope', 'Result']
        row_h = 22
        x = self.margin

        # Table header
        self.need(row_h + 4)
        self.c.setFillColor(COLOR_HEADER_BG)
        self.c.rect(x, self.y - row_h, self.content_w, row_h, fill=1, stroke=0)
        self.c.setFillColor(COLOR_ACCENT_SOFT)
        self.c.setFont('Helvetica-Bold', 8)
        cx = x
        for i, h in enumerate(headers):
            self.c.drawString(cx + 6, self.y - row_h + 6, h)
            cx += col_w[i]
        self.y -= row_h

        # Rows
        for idx, row in enumerate(rows):
            self.need(row_h)
            bg = COLOR_TABLE_ALT if idx % 2 == 0 else COLOR_SURFACE_ALT
            self.c.setFillColor(bg)
            self.c.rect(x, self.y - row_h, self.content_w, row_h, fill=1, stroke=0)
            # light border
            self.c.setStrokeColor(COLOR_PANEL_BORDER)
            self.c.setLineWidth(0.3)
            self.c.line(x, self.y - row_h, x + self.content_w, self.y - row_h)

            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica', 9)
            cx = x
            values = [row.get('area', ''), row.get('scope', ''), '']
            for i, val in enumerate(values):
                if i == 2:
                    # Green checkmark result
                    self._draw_pill(cx + 6, self.y - row_h + 4, '\u2713 TESTED', COLOR_FIXED_HEADER, font_size=7)
                else:
                    self.c.drawString(cx + 6, self.y - row_h + 7, str(val))
                cx += col_w[i]
            self.y -= row_h

        # Bottom border
        self.c.setStrokeColor(COLOR_PANEL_BORDER)
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
        header_h = 22
        row_min_h = 38
        line_h = 11
        top_pad = 8
        bottom_pad = 8
        x = self.margin
        row_gap = 6

        def draw_header():
            self.need(header_h + 4)
            self.c.setFillColor(COLOR_HEADER_BG)
            self.c.roundRect(x, self.y - header_h, self.content_w, header_h, 7, fill=1, stroke=0)
            self.c.setFillColor(COLOR_ACCENT_SOFT)
            self.c.setFont('Helvetica-Bold', 8)
            cx = x
            for i, h in enumerate(headers):
                self.c.drawString(cx + 6, self.y - header_h + 7, h)
                cx += col_w[i]
            self.y -= header_h

        draw_header()

        for idx, bug in enumerate(bugs):
            is_fixed = bug.get('fixed', False)
            btype = bug.get('type', 'BUG').upper()
            severity = bug.get('severity', '').upper()
            title = bug.get('title', '')
            area = bug.get('area', '')
            build = bug.get('fixed_build', '')
            area_lines = wrap_pdf_text((area or 'General').upper(), 'Helvetica-Bold', 6.6, col_w[2] - 12, max_lines=1)
            title_lines = wrap_pdf_text(title or 'Untitled finding', 'Helvetica-Bold', 9.4, col_w[2] - 12, max_lines=2)

            if is_fixed:
                status_text = '\u2713 FIXED'
                if build:
                    status_text += f' {build}'
            else:
                status_text = 'Open'

            content_lines = 1 + len(title_lines)
            row_h = max(row_min_h, top_pad + 8 + content_lines * line_h + bottom_pad)

            if self.y - (row_h + row_gap) < self.margin + 40:
                self.new_page()
                draw_header()

            box_x = x + 2
            box_w = self.content_w - 4
            if is_fixed:
                self.c.setFillColor(COLOR_FIXED_BG)
            else:
                self.c.setFillColor(COLOR_TABLE_ALT if idx % 2 == 0 else COLOR_SURFACE_ALT)
            self.c.roundRect(box_x, self.y - row_h, box_w, row_h, 8, fill=1, stroke=0)
            accent_color = COLOR_FIXED_HEADER if is_fixed else get_severity_color('SUGGESTION' if btype == 'SUGGESTION' else severity)
            self.c.setFillColor(accent_color)
            self.c.roundRect(box_x, self.y - row_h, 5, row_h, 2.5, fill=1, stroke=0)

            self.c.setStrokeColor(COLOR_PANEL_BORDER)
            self.c.setLineWidth(0.3)
            self.c.roundRect(box_x, self.y - row_h, box_w, row_h, 8, fill=0, stroke=1)

            cx = x
            text_y = self.y - top_pad

            # # column
            self.c.setFillColor(COLOR_TEXT_MUTED)
            self.c.setFont('Helvetica-Bold', 9)
            num_str = str(idx + 1)
            num_y = self.y - row_h / 2 - 3
            self.c.drawString(cx + 8, num_y, num_str)
            cx += col_w[0]

            # Type/Severity badge — same style as header bar: 9pt font, dynamic width
            badge_label = btype if btype == 'SUGGESTION' else severity
            badge_color = get_severity_color(badge_label if btype == 'SUGGESTION' else severity)
            dot = '\u25cf '
            label_text = f'{dot}{badge_label}' if btype != 'SUGGESTION' else 'SUGGESTION'
            by = self.y - row_h / 2 - 8
            self._draw_pill(cx + 4, by, label_text, badge_color, font_size=8)
            cx += col_w[1]

            # Issue Area / Title
            self.c.setFillColor(COLOR_TEXT_MUTED)
            self.c.setFont('Helvetica-Bold', 6.6)
            line_y = text_y
            self.c.drawString(cx + 6, line_y - 2, area_lines[0])
            line_y -= 12
            self.c.setFillColor(COLOR_TEXT)
            self.c.setFont('Helvetica-Bold', 9.4)
            for line in title_lines:
                self.c.drawString(cx + 6, line_y - line_h + 4, line)
                line_y -= line_h
            cx += col_w[2]

            # Status
            status_color = COLOR_FIXED_HEADER if is_fixed else COLOR_HEADER_BG_SOFT
            status_label = status_text.upper()
            self._draw_pill(cx + 6, self.y - row_h / 2 - 8, status_label, status_color, font_size=7.5)

            self.y -= row_h + row_gap

        self.c.setStrokeColor(COLOR_PANEL_BORDER)
        self.c.setLineWidth(0.5)
        self.c.line(x, self.y, x + self.content_w, self.y)
        self.y -= 4

    # ── Bug detail header bar ─────────────────────────────────────────────────
    def draw_bug_header(self, num, btype, severity, title, area='', is_fixed=False):
        layout = self._bug_header_layout(btype, severity, title, area, is_fixed)
        bar_h = layout['height']
        x = self.margin

        bg = COLOR_FIXED_HEADER if is_fixed else COLOR_HEADER_BG
        self.c.setFillColor(bg)
        self.c.roundRect(x, self.y - bar_h, self.content_w, bar_h, 8, fill=1, stroke=0)
        self.c.setFillColor(COLOR_ACCENT if not is_fixed else COLOR_ACCENT_SOFT)
        self.c.roundRect(x, self.y - bar_h, 8, bar_h, 5, fill=1, stroke=0)

        cur_x = x + 14

        # Left pill: "BUG #N" or "SUGGESTION #N"
        is_suggestion = layout['is_suggestion']
        num_label = f'{layout["num_label"]} #{num}'
        pill_y = self.y - 18
        nw = self._draw_pill(cur_x, pill_y, num_label, layout['pill_color'], font_size=8)
        cur_x += nw + 8

        # Severity badge (BUG only, not SUGGESTION)
        if layout['sev_label']:
            sev_color = get_severity_color(severity)
            sw = self._draw_pill(cur_x, pill_y, layout['sev_label'], sev_color, font_size=8)
            cur_x += sw + 10

        # Title
        self.c.setFillColor(COLOR_WHITE)
        self.c.setFont('Helvetica-Bold', 10.6)
        title_y = self.y - 27
        if len(layout['title_lines']) > 1:
            title_y = self.y - 21
        for line in layout['title_lines']:
            self.c.drawString(layout['x'], title_y, line)
            title_y -= 12

        # Fixed indicator overlay
        if is_fixed:
            self.c.setFillColor(COLOR_WHITE)
            self.c.setFont('Helvetica-Bold', 8)
            fixed_txt = '\u2713 FIXED'
            self.c.drawRightString(x + self.content_w - 10, self.y - 19, fixed_txt)

        self.y -= bar_h + 6

    # ── WHAT / WHERE / HOW block ──────────────────────────────────────────────
    def _wwh_text_metrics(self):
        label_w = 55
        text_x_offset = 6
        text_w = self.content_w - label_w - text_x_offset - 4
        font_size = 9
        line_h = 13
        pad = 6
        return label_w, text_x_offset, text_w, font_size, line_h, pad

    def _wwh_block_lines(self, text):
        _, _, text_w, font_size, _, _ = self._wwh_text_metrics()
        lines = simpleSplit(text, 'Helvetica', font_size, text_w)
        return lines or ['']

    def _wwh_block_height(self, text):
        _, _, _, _, line_h, pad = self._wwh_text_metrics()
        lines = self._wwh_block_lines(text)
        return max(len(lines), 1) * line_h + pad * 2

    def _wwh_height(self, what, where, how, is_fixed=False, build_str=''):
        total = 6 * 2  # outer padding
        for text in (what, where, how):
            total += self._wwh_block_height(text) + 4
        if is_fixed and build_str:
            total += 22
        return total

    def draw_what_where_how(self, what, where, how, is_fixed=False, build_str=''):
        label_w, text_x_offset, _, font_size, line_h, pad = self._wwh_text_metrics()

        blocks = [('WHAT:', what), ('WHERE:', where), ('HOW:', how)]
        x = self.margin

        for label, text in blocks:
            bh = self._wwh_block_height(text)
            lines = self._wwh_block_lines(text)
            self.need(bh)

            # Label background
            self.c.setFillColor(COLOR_WHITE)
            self.c.roundRect(x, self.y - bh, self.content_w, bh, 6, fill=1, stroke=0)
            self.c.setFillColor(WWH_LABEL_COLORS.get(label, COLOR_LABEL_BG))
            self.c.roundRect(x, self.y - bh, label_w, bh, 6, fill=1, stroke=0)
            self.c.setStrokeColor(COLOR_PANEL_BORDER)
            self.c.setLineWidth(0.3)
            self.c.roundRect(x, self.y - bh, self.content_w, bh, 6, fill=0, stroke=1)

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
    def _description_layout(self, text):
        font_size = 8.5
        line_h = 11.5
        text_x = self.margin + 20
        right_pad = 20
        text_w = self.content_w - (text_x - self.margin) - right_pad
        top_pad = 30
        bottom_pad = 14
        lines = simpleSplit(text, 'Helvetica', font_size, text_w)
        return {
            'font_size': font_size,
            'line_h': line_h,
            'text_x': text_x,
            'text_w': text_w,
            'top_pad': top_pad,
            'bottom_pad': bottom_pad,
            'lines': lines or [''],
        }

    def draw_description(self, text):
        if not text or not text.strip():
            return
        layout = self._description_layout(text)
        font_size = layout['font_size']
        line_h = layout['line_h']
        lines = layout['lines']
        top_pad = layout['top_pad']
        bottom_pad = layout['bottom_pad']
        text_x = layout['text_x']
        box_h = len(lines) * line_h + top_pad + bottom_pad
        self.need(box_h + 4)
        self.y -= 8
        box_top = self.y
        box_y = box_top - box_h
        self.c.setFillColor(COLOR_SURFACE)
        self.c.roundRect(self.margin, box_y, self.content_w, box_h, 7, fill=1, stroke=0)
        self.c.setFillColor(COLOR_ACCENT_SOFT)
        self.c.roundRect(self.margin + 14, box_top - 18, 82, 13, 6, fill=1, stroke=0)
        self.c.setFillColor(COLOR_HEADER_BG)
        self.c.setFont('Helvetica-Bold', 6.8)
        self.c.drawString(self.margin + 22, box_top - 14, 'DESCRIPTION')
        self.c.setFillColor(COLOR_ACCENT)
        self.c.roundRect(self.margin + 8, box_y + 10, 3, box_h - 20, 1.5, fill=1, stroke=0)
        self.c.setFillColor(COLOR_NARRATIVE)
        self.c.setFont('Helvetica', font_size)
        text_y = box_top - top_pad + 2
        for line in lines:
            self.c.drawString(text_x, text_y, line)
            text_y -= line_h
        self.y = box_y - 6

    # ── Fixed verified note ───────────────────────────────────────────────────
    def draw_fixed_note(self, build_str):
        if not build_str:
            return
        self.need(20)
        self.y -= 2
        note = f'\u2713 Verified fixed in build {build_str}'
        self._draw_pill(self.margin, self.y - 10, note, COLOR_FIXED_HEADER, font_size=8, pad_x=8, height=18)
        self.y -= 14

    # ── Screenshots ───────────────────────────────────────────────────────────
    def draw_screenshots(self, image_paths, reserve_after=0):
        if not image_paths:
            return

        self.need(26)
        self.y -= 2
        self.c.setFillColor(COLOR_TEXT_MUTED)
        self.c.setFont('Helvetica-Bold', 7)
        self.c.drawString(self.margin, self.y, 'VISUAL EVIDENCE')
        meta = f'{len(image_paths)} screenshot' + ('s' if len(image_paths) != 1 else '')
        self.c.setFillColor(COLOR_NARRATIVE)
        self.c.setFont('Helvetica', 8)
        self.c.drawString(self.margin + 88, self.y, meta)
        self.c.setStrokeColor(COLOR_SEPARATOR)
        self.c.setLineWidth(0.6)
        self.c.line(self.margin, self.y - 6, self.margin + self.content_w, self.y - 6)
        self.y -= 14

        def _is_portrait(path):
            try:
                with Image.open(path) as img:
                    img = _normalize_image_orientation(img)
                    iw, ih = img.size
                return ih > iw
            except Exception:
                return True  # treat unknown as portrait

        i = 0
        while i < len(image_paths):
            path1 = image_paths[i]
            # Only pair two consecutive portraits side-by-side;
            # landscape/square images always display full-width one at a time
            is_pair = (
                _is_portrait(path1)
                and i + 1 < len(image_paths)
                and _is_portrait(image_paths[i + 1])
            )
            is_last_block = (i + (2 if is_pair else 1)) >= len(image_paths)
            block_reserve = reserve_after if is_last_block else 0

            if is_pair:
                self._draw_two_images(path1, image_paths[i + 1], reserve_after=block_reserve)
                i += 2
            else:
                self._draw_one_image(path1, reserve_after=block_reserve)
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

    def _available_content_height(self):
        return max(self.y - (self.margin + 40), 0)

    @staticmethod
    def _scale_dims_to_height(dw, dh, max_h):
        scale = max_h / dh
        return dw * scale, dh * scale

    def _target_block_height(self, natural_h, min_h, reserve_after=0):
        available_h = self._available_content_height()
        preferred_h = available_h - reserve_after if reserve_after else available_h

        if preferred_h >= min_h:
            return min(natural_h, preferred_h)
        if available_h >= min_h:
            return min(natural_h, available_h)
        return None

    def _prepare_image(self, path, crop_aspect=None):
        """Convert image to RGB JPEG. crop_aspect=(w/h) crops to that ratio if given."""
        with Image.open(path) as img:
            img = _normalize_image_orientation(img)
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

    def _draw_one_image(self, path, reserve_after=0):
        try:
            with Image.open(path) as img:
                img = _normalize_image_orientation(img)
                iw, ih = img.size
            max_w = 240 if ih > iw else 400
            dw, dh = self._img_dims(iw, ih, max_w)
            frame_total_h = dh + self.IMAGE_CARD_PAD * 2
            min_h = self.MIN_TRAILING_IMAGE_HEIGHT if reserve_after else self.MIN_INLINE_IMAGE_HEIGHT
            target_h = self._target_block_height(
                frame_total_h,
                min_h + self.IMAGE_CARD_PAD * 2,
                reserve_after=reserve_after,
            )

            # If the image nearly fits, scale it to the remaining space instead of
            # forcing a page break that would leave the previous page visibly empty.
            if target_h is None:
                self.need(frame_total_h + 12)
            elif target_h < frame_total_h:
                image_target_h = max(target_h - self.IMAGE_CARD_PAD * 2, 1)
                dw, dh = self._scale_dims_to_height(dw, dh, image_target_h)
                frame_total_h = dh + self.IMAGE_CARD_PAD * 2

            tmp = self._prepare_image(path)
            cx = self.margin + (self.content_w - dw) / 2
            self._draw_image_frame(cx, self.y - dh, dw, dh)
            self.c.drawImage(tmp, cx, self.y - dh, width=dw, height=dh)
            self.y -= frame_total_h + 8
            try:
                os.remove(tmp)
            except Exception:
                pass
        except Exception as e:
            print(f'Image error {path}: {e}')

    def _draw_two_images(self, path1, path2, reserve_after=0):
        MAX_W = 210
        GAP = 10
        try:
            with Image.open(path1) as img:
                img = _normalize_image_orientation(img)
                iw1, ih1 = img.size
            with Image.open(path2) as img:
                img = _normalize_image_orientation(img)
                iw2, ih2 = img.size
            dw1, dh1 = self._img_dims(iw1, ih1, MAX_W)
            dw2, dh2 = self._img_dims(iw2, ih2, MAX_W)
            display_h = max(dh1, dh2)
            frame_total_h = display_h + self.IMAGE_CARD_PAD * 2
            min_h = self.MIN_TRAILING_PAIR_HEIGHT if reserve_after else self.MIN_INLINE_PAIR_HEIGHT
            target_h = self._target_block_height(
                frame_total_h,
                min_h + self.IMAGE_CARD_PAD * 2,
                reserve_after=reserve_after,
            )

            if target_h is None:
                self.need(frame_total_h + 12)
            elif target_h < frame_total_h:
                image_target_h = max(target_h - self.IMAGE_CARD_PAD * 2, 1)
                scale = image_target_h / display_h
                dw1 *= scale
                dh1 *= scale
                dw2 *= scale
                dh2 *= scale
                display_h = image_target_h
                frame_total_h = display_h + self.IMAGE_CARD_PAD * 2

            tmp1 = self._prepare_image(path1)
            tmp2 = self._prepare_image(path2)
            total_w = dw1 + GAP + dw2
            x1 = self.margin + (self.content_w - total_w) / 2
            x2 = x1 + dw1 + GAP
            self._draw_image_frame(x1, self.y - display_h, total_w, display_h)
            self.c.drawImage(tmp1, x1, self.y - dh1, width=dw1, height=dh1)
            self.c.drawImage(tmp2, x2, self.y - dh2, width=dw2, height=dh2)
            self.y -= frame_total_h + 8
            for tmp in (tmp1, tmp2):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        except Exception as e:
            print(f'Image pair error: {e}')

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
        if not text or not text.strip():
            return

        text = clean_text(text)
        font_size = 9.5
        line_h = 14
        card_text_w = self.content_w - 36
        lines = simpleSplit(text, 'Helvetica', font_size, card_text_w) or ['']
        first_page = True

        while lines:
            self.new_page()
            self.draw_section_title('Overall Assessment', top_pad=8, bot_pad=16 if first_page else 12)

            card_top_pad = 34
            card_bottom_pad = 18
            available_h = self.y - (self.margin + 44)
            lines_fit = max(1, int((available_h - card_top_pad - card_bottom_pad) / line_h))
            chunk = lines[:lines_fit]
            lines = lines[lines_fit:]
            card_h = card_top_pad + card_bottom_pad + len(chunk) * line_h
            self.need(card_h + 4)

            card_top = self.y
            card_y = card_top - card_h
            self.c.setFillColor(COLOR_SURFACE_ALT)
            self.c.roundRect(self.margin, card_y, self.content_w, card_h, 10, fill=1, stroke=0)
            self.c.setFillColor(COLOR_ACCENT_SOFT)
            self.c.roundRect(self.margin + 14, card_top - 22, 90, 14, 7, fill=1, stroke=0)
            self.c.setFillColor(COLOR_HEADER_BG)
            self.c.setFont('Helvetica-Bold', 7)
            self.c.drawString(self.margin + 23, card_top - 17, 'RELEASE READINESS')
            self.c.setFillColor(COLOR_ACCENT)
            self.c.roundRect(self.margin + 14, card_y + 14, 4, card_h - 28, 2, fill=1, stroke=0)
            self.c.setFillColor(COLOR_NARRATIVE)
            self.c.setFont('Helvetica', font_size)
            text_y = card_top - card_top_pad
            for line in chunk:
                self.c.drawString(self.margin + 28, text_y, line)
                text_y -= line_h

            self.y = card_y - 8
            first_page = False


# ─── PDF Builder ──────────────────────────────────────────────────────────────
def build_pdf(data, uploaded_files, output_path):
    c = canvas.Canvas(output_path, pagesize=A4)
    pw = PageWriter(c, PAGE_W, PAGE_H, MARGIN)

    app_name = data.get('app_name', 'App')
    app_desc = data.get('app_desc', '')
    device = data.get('device', '')
    report_date_label = data.get('report_date_label', datetime.now().strftime('%d.%m.%Y'))
    coverage_rows = data.get('coverage_rows', [])
    bugs = data.get('bugs', [])
    sorted_bug_entries = sort_bug_entries_for_pdf(bugs, uploaded_files)
    assessment = data.get('assessment', '')

    # ── Page 1: Cover ────────────────────────────────────────────────────────
    pw.draw_cover_header(app_name, app_desc, device, report_date_label)

    pw.draw_section_title('Test Coverage', top_pad=18, bot_pad=6)
    pw.draw_test_coverage_table(coverage_rows)

    if sorted_bug_entries:
        pw.draw_section_title('Bug Summary', top_pad=27, bot_pad=6)
        pw.draw_bug_summary_table([entry['bug'] for entry in sorted_bug_entries])

    # ── Pages 2+: Bug Details ─────────────────────────────────────────────────
    for idx, entry in enumerate(sorted_bug_entries):
        bug = entry['bug']
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
        screenshots = entry['screenshots']

        # Reserve space only for the bug intro so the next item can start lower on
        # the page when it still has room for a clean opening block.
        sep_h = 14
        text_block_need = estimate_bug_start_height(pw, bug)
        next_bug_start_need = 0
        if idx + 1 < len(sorted_bug_entries):
            next_bug_start_need = sep_h + estimate_bug_start_height(pw, sorted_bug_entries[idx + 1]['bug'])

        if idx == 0:
            pw.need(text_block_need)
        else:
            if pw.y - (sep_h + text_block_need) < MARGIN + 40:
                pw.new_page()
            else:
                pw.draw_separator()

        pw.draw_bug_header(idx + 1, btype, severity, title, area=area, is_fixed=is_fixed)

        # WHAT / WHERE / HOW
        pw.draw_what_where_how(what, where, how, is_fixed=is_fixed, build_str=build_str)

        # Fixed note
        if is_fixed and build_str:
            pw.draw_fixed_note(build_str)

        # Narrative description
        pw.draw_description(description)

        # Screenshots
        if screenshots:
            pw.draw_screenshots(screenshots, reserve_after=next_bug_start_need)

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
    cleanup_old_reports()

    # Parse form data
    app_name = request.form.get('app_name', 'App').strip()
    # Strip accidental "QA Test Report — " prefix if user typed the full title
    app_name = re.sub(r'^QA\s+Test\s+Report\s*[\u2014\-]\s*', '', app_name).strip() or 'App'
    app_desc = request.form.get('app_desc', '').strip()
    device = request.form.get('device', '').strip()
    report_date_mode = request.form.get('report_date_mode', 'single').strip()
    report_date = request.form.get('report_date', '').strip()
    report_start_date = request.form.get('report_start_date', '').strip()
    report_end_date = request.form.get('report_end_date', '').strip()
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
    out_path = None
    json_path = None

    try:
        report_date_context = build_report_date_context(
            report_date_mode,
            report_date,
            report_start_date,
            report_end_date,
        )

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

            # Validate screenshots on the backend so bad uploads fail fast and clearly.
            file_key = f'bug_screenshots_{i}'
            files = request.files.getlist(file_key)
            bug_images = []
            for f in files:
                if f and f.filename:
                    ext = validate_image_upload(f)
                    optimized_bytes, ext = optimize_uploaded_image(f, ext)
                    fname = f'{uuid.uuid4().hex}{ext}'
                    fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                    with open(fpath, 'wb') as image_file:
                        image_file.write(optimized_bytes)
                    saved_paths.append(fpath)
                    bug_images.append(fpath)
            if bug_images:
                uploaded_files[f'bug_{i}'] = bug_images

        data = {
            'app_name': app_name,
            'app_desc': app_desc,
            'device': device,
            'report_date_label': report_date_context['label'],
            'coverage_rows': coverage_rows,
            'bugs': bugs,
            'assessment': assessment,
        }

        # Generate PDF + JSON
        slug = f'{build_report_slug(app_name)}_{uuid.uuid4().hex[:6]}'
        out_name = f'{app.config["REPORT_PREFIX"]}{slug}.pdf'
        out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_name)
        json_name = f'{app.config["REPORT_PREFIX"]}{slug}.json'
        json_path = os.path.join(app.config['OUTPUT_FOLDER'], json_name)

        build_pdf(data, uploaded_files, out_path)

        report_json = {
            'app_name': app_name,
            'app_desc': app_desc,
            'device': device,
            'report_date_mode': report_date_context['mode'],
            'report_date': report_date_context['date'],
            'report_start_date': report_date_context['start_date'],
            'report_end_date': report_date_context['end_date'],
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
    except ValueError as exc:
        cleanup_paths(saved_paths)
        cleanup_paths([out_path, json_path])
        return jsonify({'error': str(exc)}), 400
    except Exception:
        cleanup_paths([out_path, json_path])
        raise
    finally:
        cleanup_paths(saved_paths)

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


@app.errorhandler(413)
def handle_request_too_large(_error):
    limit_str = format_upload_limit()
    return jsonify({'error': f'Uploaded files are too large. Max total upload size is {limit_str}.'}), 413


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5001)
    parser.add_argument('--debug', action='store_true')
    args, _ = parser.parse_known_args()
    app.run(debug=args.debug, port=args.port)
