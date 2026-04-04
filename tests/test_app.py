import io
import json
import os
import tempfile
import time
import unittest

from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from werkzeug.datastructures import FileStorage

from app import (
    MARGIN,
    PAGE_H,
    PAGE_W,
    PageWriter,
    app as flask_app,
    build_cover_subtitle,
    estimate_bug_start_height,
    optimize_uploaded_image,
    sort_bug_entries_for_pdf,
    validate_image_upload,
)


class ReportGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.upload_dir = os.path.join(self.tmpdir.name, 'uploads')
        self.output_dir = os.path.join(self.tmpdir.name, 'output')
        os.makedirs(self.upload_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

        self.original_config = {
            'TESTING': flask_app.config.get('TESTING'),
            'UPLOAD_FOLDER': flask_app.config['UPLOAD_FOLDER'],
            'OUTPUT_FOLDER': flask_app.config['OUTPUT_FOLDER'],
            'MAX_CONTENT_LENGTH': flask_app.config['MAX_CONTENT_LENGTH'],
            'REPORT_TTL_SECONDS': flask_app.config['REPORT_TTL_SECONDS'],
        }

        flask_app.config.update(
            TESTING=True,
            UPLOAD_FOLDER=self.upload_dir,
            OUTPUT_FOLDER=self.output_dir,
            MAX_CONTENT_LENGTH=500 * 1024 * 1024,
            REPORT_TTL_SECONDS=60,
        )
        self.client = flask_app.test_client()

    def tearDown(self):
        flask_app.config.update(self.original_config)
        self.tmpdir.cleanup()

    @staticmethod
    def png_upload(filename='shot.png'):
        image = Image.new('RGB', (20, 20), color=(255, 0, 0))
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        buf.seek(0)
        return buf, filename

    @staticmethod
    def noisy_png_bytes(size=(1000, 2200)):
        image = Image.effect_noise(size, 95).convert('RGB')
        buf = io.BytesIO()
        image.save(buf, format='PNG')
        return buf.getvalue()

    def test_generate_sanitizes_app_name_for_output_files(self):
        response = self.client.post('/generate', data={'app_name': 'A/B', 'bug_count': '0'})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('QA_Report_A_B_', payload['pdf_name'])
        self.assertNotIn('/', payload['pdf_name'])
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, payload['pdf_name'])))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, payload['json_name'])))

    def test_generate_supports_date_range_and_persists_it_in_json(self):
        response = self.client.post(
            '/generate',
            data={
                'app_name': 'Range Test',
                'report_date_mode': 'range',
                'report_start_date': '2026-04-01',
                'report_end_date': '2026-04-03',
                'bug_count': '0',
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        json_path = os.path.join(self.output_dir, payload['json_name'])
        with open(json_path, 'r', encoding='utf-8') as fh:
            exported = json.load(fh)

        self.assertEqual(exported['report_date_mode'], 'range')
        self.assertEqual(exported['report_start_date'], '2026-04-01')
        self.assertEqual(exported['report_end_date'], '2026-04-03')
        self.assertEqual(exported['report_date'], '')

    def test_generate_rejects_invalid_date_range(self):
        response = self.client.post(
            '/generate',
            data={
                'app_name': 'Bad Range',
                'report_date_mode': 'range',
                'report_start_date': '2026-04-05',
                'report_end_date': '2026-04-03',
                'bug_count': '0',
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()['error'],
            'End date must be the same as or later than start date.',
        )

    def test_generate_rejects_invalid_image_uploads(self):
        response = self.client.post(
            '/generate',
            data={
                'app_name': 'Test App',
                'bug_count': '1',
                'bug_type_0': 'BUG',
                'bug_severity_0': 'HIGH',
                'bug_title_0': 'Broken screenshot',
                'bug_area_0': 'Upload',
                'bug_what_0': 'Bad file',
                'bug_where_0': 'Form',
                'bug_how_0': 'Attach invalid file',
                'bug_description_0': 'Should fail clearly',
                'bug_fixed_0': 'false',
                'bug_screenshots_0': (io.BytesIO(b'not an image'), 'fake.txt'),
            },
            content_type='multipart/form-data',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('not a valid image', response.get_json()['error'])
        self.assertEqual(os.listdir(self.upload_dir), [])
        self.assertEqual(os.listdir(self.output_dir), [])

    def test_optimize_uploaded_image_reduces_large_files_and_keeps_aspect_ratio(self):
        original_bytes = self.noisy_png_bytes()
        file_storage = FileStorage(stream=io.BytesIO(original_bytes), filename='large.png')

        original_ext = validate_image_upload(file_storage)
        optimized_bytes, optimized_ext = optimize_uploaded_image(file_storage, original_ext)

        self.assertGreater(len(original_bytes), flask_app.config['SCREENSHOT_TARGET_MAX_BYTES'])
        self.assertLess(len(optimized_bytes), len(original_bytes))
        self.assertEqual(optimized_ext, '.webp')

        with Image.open(io.BytesIO(original_bytes)) as original_img:
            original_size = original_img.size
        with Image.open(io.BytesIO(optimized_bytes)) as optimized_img:
            optimized_size = optimized_img.size

        self.assertLessEqual(optimized_size[0], original_size[0])
        self.assertLessEqual(optimized_size[1], original_size[1])
        self.assertAlmostEqual(
            optimized_size[0] / optimized_size[1],
            original_size[0] / original_size[1],
            places=2,
        )
        self.assertLess(len(optimized_bytes), len(original_bytes) * 0.5)

    def test_optimize_uploaded_image_keeps_small_files_as_is(self):
        original_buf, _ = self.png_upload('small.png')
        original_bytes = original_buf.getvalue()
        file_storage = FileStorage(stream=io.BytesIO(original_bytes), filename='small.png')

        original_ext = validate_image_upload(file_storage)
        optimized_bytes, optimized_ext = optimize_uploaded_image(file_storage, original_ext)

        self.assertEqual(optimized_ext, '.png')
        self.assertEqual(optimized_bytes, original_bytes)

    def test_generate_cleans_stale_reports(self):
        stale_pdf = os.path.join(self.output_dir, 'QA_Report_old.pdf')
        stale_json = os.path.join(self.output_dir, 'QA_Report_old.json')
        for path in (stale_pdf, stale_json):
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('stale')
            old_time = time.time() - 120
            os.utime(path, (old_time, old_time))

        flask_app.config['REPORT_TTL_SECONDS'] = 60

        response = self.client.post('/generate', data={'app_name': 'Fresh App', 'bug_count': '0'})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(os.path.exists(stale_pdf))
        self.assertFalse(os.path.exists(stale_json))

    def test_request_too_large_returns_json_error(self):
        flask_app.config['MAX_CONTENT_LENGTH'] = 200

        response = self.client.post(
            '/generate',
            data={
                'app_name': 'Big Upload',
                'bug_count': '1',
                'bug_type_0': 'BUG',
                'bug_severity_0': 'HIGH',
                'bug_title_0': 'Large file',
                'bug_area_0': 'Upload',
                'bug_what_0': 'Too large',
                'bug_where_0': 'Form',
                'bug_how_0': 'Upload a large image',
                'bug_description_0': 'Should return 413',
                'bug_fixed_0': 'false',
                'bug_screenshots_0': self.png_upload('large.png'),
            },
            content_type='multipart/form-data',
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            response.get_json()['error'],
            'Uploaded files are too large. Max total upload size is 200 bytes.',
        )

    def test_generate_accepts_large_screenshot_after_compression(self):
        response = self.client.post(
            '/generate',
            data={
                'app_name': 'Compressed Upload',
                'bug_count': '1',
                'bug_type_0': 'BUG',
                'bug_severity_0': 'MEDIUM',
                'bug_title_0': 'Large screenshot',
                'bug_area_0': 'Upload',
                'bug_what_0': 'Large image upload',
                'bug_where_0': 'Form',
                'bug_how_0': 'Upload a large screenshot',
                'bug_description_0': 'Should still generate successfully',
                'bug_fixed_0': 'false',
                'bug_screenshots_0': (io.BytesIO(self.noisy_png_bytes()), 'large.png'),
            },
            content_type='multipart/form-data',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, payload['pdf_name'])))

    def test_pdf_bug_sorting_orders_by_severity_then_suggestions(self):
        bugs = [
            {'type': 'BUG', 'severity': 'LOW', 'title': 'Low bug'},
            {'type': 'SUGGESTION', 'severity': 'HIGH', 'title': 'Suggestion'},
            {'type': 'BUG', 'severity': 'HIGH', 'title': 'High bug'},
            {'type': 'BUG', 'severity': 'MEDIUM', 'title': 'Medium bug'},
            {'type': 'BUG', 'severity': 'HIGH', 'title': 'Second high bug'},
        ]
        uploaded_files = {
            'bug_0': ['low.png'],
            'bug_1': ['suggestion.png'],
            'bug_2': ['high.png'],
            'bug_3': ['medium.png'],
            'bug_4': ['second-high.png'],
        }

        sorted_entries = sort_bug_entries_for_pdf(bugs, uploaded_files)

        self.assertEqual(
            [entry['bug']['title'] for entry in sorted_entries],
            ['High bug', 'Second high bug', 'Medium bug', 'Low bug', 'Suggestion'],
        )
        self.assertEqual(
            [entry['screenshots'] for entry in sorted_entries],
            [['high.png'], ['second-high.png'], ['medium.png'], ['low.png'], ['suggestion.png']],
        )

    def test_bug_start_height_depends_on_header_and_what_only(self):
        pdf = canvas.Canvas(io.BytesIO())
        writer = PageWriter(pdf, PAGE_W, PAGE_H, MARGIN)
        short_followup = {
            'what': 'Short what',
            'where': 'Short where',
            'how': 'Short how',
        }
        long_followup = {
            'what': 'Short what',
            'where': 'Very long where ' * 40,
            'how': 'Very long how ' * 40,
        }

        self.assertEqual(
            estimate_bug_start_height(writer, short_followup),
            estimate_bug_start_height(writer, long_followup),
        )

    def test_trailing_image_rule_allows_more_aggressive_scaling(self):
        pdf = canvas.Canvas(io.BytesIO())
        writer = PageWriter(pdf, PAGE_W, PAGE_H, MARGIN)
        writer.y = writer.margin + 40 + 190

        self.assertIsNone(writer._target_block_height(400, writer.MIN_INLINE_IMAGE_HEIGHT, reserve_after=20))
        self.assertEqual(
            writer._target_block_height(400, writer.MIN_TRAILING_IMAGE_HEIGHT, reserve_after=20),
            170,
        )

    def test_cover_subtitle_avoids_repeating_app_name_content(self):
        self.assertEqual(
            build_cover_subtitle('PlateBird — Calorie Tracker · iOS App Testing', 'Calorie Tracker'),
            '',
        )

    def test_description_layout_wraps_to_inner_card_width(self):
        pdf = canvas.Canvas(io.BytesIO())
        writer = PageWriter(pdf, PAGE_W, PAGE_H, MARGIN)
        text = (
            'This is a longer description line that should wrap inside the description '
            'card and never overflow beyond its right padding.'
        )

        layout = writer._description_layout(text)

        self.assertGreater(layout['text_w'], 0)
        for line in layout['lines']:
            self.assertLessEqual(
                pdfmetrics.stringWidth(line, 'Helvetica', layout['font_size']),
                layout['text_w'] + 0.1,
            )


if __name__ == '__main__':
    unittest.main()
