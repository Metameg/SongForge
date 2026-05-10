import contextlib
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright


DUMMY_MP3 = b"ID3" + (b"\x00" * 256)


class _AppHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self.send_error(404)
            return

        params = urllib.parse.parse_qs(parsed.query)
        download_url = params.get("download", [""])[0]
        safe_download_url = download_url.replace('"', '&quot;')
        body = f"""<!doctype html>
<html>
  <body>
    <a id=\"downloadSongBtn\" href=\"#\">Download My Song</a>
    <script>
      const btn = document.getElementById('downloadSongBtn');
      btn.href = \"{safe_download_url}\";
      btn.addEventListener('click', async (event) => {{
        event.preventDefault();
        const response = await fetch(btn.href, {{ mode: 'cors' }});
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const disposition = response.headers.get('content-disposition') || '';
        const filenameMatch = disposition.match(/filename=\"?([^\";]+)\"?/i);
        const filename = (filenameMatch && filenameMatch[1]) || 'song.mp3';
        const downloadLink = document.createElement('a');
        downloadLink.href = objectUrl;
        downloadLink.download = filename;
        document.body.appendChild(downloadLink);
        downloadLink.click();
        downloadLink.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
      }});
    </script>
  </body>
</html>""".encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class _FileHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/song.mp3":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Disposition", 'attachment; filename="my-song.mp3"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Disposition")
        self.send_header("Content-Length", str(len(DUMMY_MP3)))
        self.end_headers()
        self.wfile.write(DUMMY_MP3)

    def log_message(self, *_args):
        return


@contextlib.contextmanager
def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_cross_origin_href_click_triggers_download(tmp_path):
    with _serve(_FileHandler) as file_server, _serve(_AppHandler) as app_server:
        file_url = f"http://127.0.0.1:{file_server.server_port}/song.mp3"
        page_url = (
            f"http://127.0.0.1:{app_server.server_port}/?"
            f"download={urllib.parse.quote(file_url, safe='')}"
        )

        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.goto(page_url)

            with page.expect_download() as download_info:
                page.click("#downloadSongBtn")

            download = download_info.value
            assert download.suggested_filename == "my-song.mp3"

            saved_path = tmp_path / download.suggested_filename
            download.save_as(str(saved_path))
            assert saved_path.exists()
            assert saved_path.read_bytes().startswith(b"ID3")

            context.close()
            browser.close()
