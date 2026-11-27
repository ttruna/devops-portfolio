#!/usr/bin/env python3
"""Автоматичні тести для backup.sh та api_monitor.py."""

import http.server
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = LAB_DIR / "backup.sh"
MONITOR_SCRIPT = LAB_DIR / "api_monitor.py"


class TestBackupScript(unittest.TestCase):
    def test_missing_source_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res = subprocess.run(
                [str(BACKUP_SCRIPT), "--source", f"{tmp}/does_not_exist", "--dest", f"{tmp}/out"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("не існує", res.stderr)

    def test_rotation_and_spaces_in_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "my source folder"
            dest = Path(tmp) / "my backup dest"
            src.mkdir()
            dest.mkdir()
            (src / "file with spaces.txt").write_text("hello", encoding="utf-8")

            foreign = dest / "other-project-2026-01-01-000000.tar.gz"
            foreign.write_text("keep me", encoding="utf-8")

            for _ in range(5):
                res = subprocess.run(
                    [str(BACKUP_SCRIPT), "--source", str(src), "--dest", str(dest), "--keep", "3"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, res.stderr)

            own_archives = list(dest.glob("my source folder-*.tar.gz"))
            self.assertEqual(len(own_archives), 3)
            self.assertTrue(foreign.exists())

            dry_res = subprocess.run(
                [
                    str(BACKUP_SCRIPT),
                    "--source",
                    str(src),
                    "--dest",
                    str(dest),
                    "--keep",
                    "2",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(dry_res.returncode, 0)
            self.assertIn("DRY-RUN", dry_res.stdout)
            self.assertEqual(len(list(dest.glob("my source folder-*.tar.gz"))), 3)


class _TestHTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ok":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/slow":
            time.sleep(0.25)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"slow")
        else:
            self.send_response(503)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class TestApiMonitor(unittest.TestCase):
    def test_failure_classifications(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TestHTTPHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]

        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "targets.json"
                out_path = Path(tmp) / "results.json"
                log_path = Path(tmp) / "monitor.log"

                cfg = {
                    "targets": [
                        {
                            "name": "OK",
                            "url": f"http://127.0.0.1:{port}/ok",
                            "expected_status": 200,
                            "timeout": 2.0,
                            "latency_threshold_ms": 1000,
                        },
                        {
                            "name": "Slow",
                            "url": f"http://127.0.0.1:{port}/slow",
                            "expected_status": 200,
                            "timeout": 2.0,
                            "latency_threshold_ms": 50,
                        },
                        {
                            "name": "WrongCode",
                            "url": f"http://127.0.0.1:{port}/fail",
                            "expected_status": 200,
                            "timeout": 2.0,
                            "latency_threshold_ms": 1000,
                        },
                        {
                            "name": "Refused",
                            "url": f"http://127.0.0.1:{closed_port}/",
                            "expected_status": 200,
                            "timeout": 1.0,
                            "latency_threshold_ms": 1000,
                        },
                        {
                            "name": "DNS",
                            "url": "http://invalid-host-for-unit-test.invalid/",
                            "expected_status": 200,
                            "timeout": 1.0,
                            "latency_threshold_ms": 1000,
                        },
                    ]
                }
                cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

                env = os.environ.copy()
                env.pop("MONITOR_WEBHOOK_URL", None)

                res = subprocess.run(
                    [
                        "python3",
                        str(MONITOR_SCRIPT),
                        "--config",
                        str(cfg_path),
                        "--output",
                        str(out_path),
                        "--log-file",
                        str(log_path),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(res.returncode, 1)

                data = json.loads(out_path.read_text(encoding="utf-8"))
                by_name = {item["name"]: item for item in data["results"]}

                self.assertEqual(by_name["OK"]["status"], "HEALTHY")
                self.assertEqual(by_name["Slow"]["failure_type"], "Повільна відповідь")
                self.assertEqual(by_name["WrongCode"]["failure_type"], "Несподіваний код")
                self.assertEqual(by_name["Refused"]["failure_type"], "З'єднання відхилено")
                self.assertEqual(by_name["DNS"]["failure_type"], "DNS")
        finally:
            server.shutdown()
            server.server_close()

    def test_ssl_classification_unit(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("api_monitor", str(MONITOR_SCRIPT))
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        ftype, _ = mod.classify_exception(ssl.SSLCertVerificationError("certificate has expired"))
        self.assertEqual(ftype, "Помилка сертифіката")


if __name__ == "__main__":
    unittest.main()
