# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
python3 app.py              # starts on http://localhost:5001
python3 app.py --port 8000  # custom port
```

No tests or linter configured.

## Dependencies

Install manually — no requirements.txt:

```bash
pip install flask reportlab pillow
```

## Architecture

Single-file Flask backend (`app.py`) + single-page frontend (`templates/index.html`).

**Request flow:**
1. Browser POSTs multipart form to `/generate` (form fields + screenshot files)
2. Flask saves screenshots to `uploads/`, builds PDF via `PageWriter`, exports JSON
3. Both files land in `output/`, Flask returns `{ pdf_url, json_url }` as JSON
4. JS triggers two sequential `<a>` downloads (400 ms apart to avoid popup blocking)
5. `uploads/` screenshots are deleted in a `finally` block after PDF is built

**`PageWriter` class (`app.py:63`)** — all PDF layout lives here. It wraps a ReportLab canvas and tracks a `self.y` cursor for vertical position. Key conventions:
- `need(h)` checks if `h` points fit on the current page; calls `new_page()` if not
- `_img_dims(iw, ih, max_w)` — sizes images: portrait max 240 pt wide, landscape max 400 pt wide, never scales up
- Portrait images are paired 2-up (max 210 pt each, 10 pt gap) by `_draw_two_images()`
- `_first_image_block_height(paths)` pre-calculates image block height so the bug header + WWH + first screenshot never orphan across pages

**Frontend state (`templates/index.html`):**
- `bugs[]` array holds all bug data including screenshot DataURLs in memory
- No persistence — a page refresh loses all work (known limitation, no localStorage)
- `importJson(input)` repopulates form from a `.json` file; screenshots must be re-added manually

## Key files

| File | Purpose |
|------|---------|
| `app.py` | Flask routes + entire PDF generation logic |
| `templates/index.html` | Single-page form UI + vanilla JS state management |
| `static/style.css` | Dark theme |
| `output/` | Generated PDFs and JSON reports (not version-controlled) |
| `uploads/` | Temporary screenshot storage (not version-controlled) |

## Branches

| Branch | Purpose |
|--------|---------|
| `main` | Stable branch |
| `json-plus` | JSON export + import feature |
| `test-gpt` | Experimental UI improvements |

## Known issues / tech debt

- Tester name/email hardcoded in `app.py` (~lines 82, 125) — must edit code to change
- `debug=True` in `app.run()` — never deploy as-is
- `output/` files accumulate and are never cleaned up
- No server-side file size or MIME type validation on uploaded screenshots
