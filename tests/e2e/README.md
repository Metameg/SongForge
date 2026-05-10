# E2E download-link test

This test validates browser behavior for the "Download My Song" JS-driven download flow.

## What it checks
- A same-page script sets `#downloadSongBtn.href` to a cross-origin audio URL.
- Clicking the anchor uses `fetch` + `Blob` + object URL to trigger download.
- The downloaded file name comes from `Content-Disposition`.
- The cross-origin response includes CORS headers needed for JS access.

## Run locally
```bash
pip install playwright pytest
python -m playwright install chromium
pytest tests/e2e/test_download_button.py
```

The test spins up two local servers to simulate app origin and file origin (cross-origin).
