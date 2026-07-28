from __future__ import annotations

from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

import preview_server


class DisconnectingWriter:
    def write(self, _body: bytes) -> None:
        raise BrokenPipeError


class PreviewServerTests(unittest.TestCase):
    def test_client_disconnect_does_not_emit_a_server_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "index.html"
            path.write_text("<!doctype html>", encoding="utf-8")
            handler = SimpleNamespace(
                guess_type=lambda _path: "text/html",
                send_response=lambda _status: None,
                send_header=lambda _name, _value: None,
                end_headers=lambda: None,
                command="GET",
                wfile=DisconnectingWriter(),
            )
            preview_server.PreviewHandler._send_file(handler, path)


if __name__ == "__main__":
    unittest.main()
