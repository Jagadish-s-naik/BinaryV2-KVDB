"""Unit tests for kvdb CLI and multi-threaded serve protocol."""

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
ROOT = Path(__file__).resolve().parent.parent
KVDB_BIN = str(ROOT / "kvdb")

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import subprocess
import tempfile
import unittest


class TestCLICommands(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "cli_test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run_cli(self, *args: str) -> tuple[int, str]:
        cmd = [sys.executable, KVDB_BIN] + list(args)
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout.strip()

    def test_cli_full_flow(self):
        # 1. open
        rc, out = self._run_cli("open", self.db_path)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("OK keys=0"))

        # 2. put
        rc, out = self._run_cli("put", self.db_path, "k1", "v1")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "OK")

        # put with spaces in value
        rc, out = self._run_cli("put", self.db_path, "phrase", "hello brave new world")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "OK")

        # 3. get
        rc, out = self._run_cli("get", self.db_path, "k1")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "v1")

        rc, out = self._run_cli("get", self.db_path, "phrase")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "hello brave new world")

        # get missing
        rc, out = self._run_cli("get", self.db_path, "missing_key")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "NOTFOUND")

        # 4. del
        rc, out = self._run_cli("del", self.db_path, "k1")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "OK")

        rc, out = self._run_cli("get", self.db_path, "k1")
        self.assertEqual(out, "NOTFOUND")

        # 5. batch
        batch_file = os.path.join(self.tmpdir.name, "batch.txt")
        Path(batch_file).write_text(
            "put city San Francisco\n"
            "put user Alice Smith\n"
            "del phrase\n",
            encoding="utf-8",
        )
        rc, out = self._run_cli("batch", self.db_path, batch_file)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "OK")

        rc, out = self._run_cli("get", self.db_path, "city")
        self.assertEqual(out, "San Francisco")
        rc, out = self._run_cli("get", self.db_path, "phrase")
        self.assertEqual(out, "NOTFOUND")

        # 6. dump
        rc, out = self._run_cli("dump", self.db_path)
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "city San Francisco")
        self.assertEqual(lines[1], "user Alice Smith")

        # dump --hex
        rc, out = self._run_cli("dump", self.db_path, "--hex")
        self.assertEqual(rc, 0)
        hex_lines = out.splitlines()
        self.assertEqual(len(hex_lines), 2)
        # "city".hex() = 63697479
        self.assertTrue(hex_lines[0].startswith("63697479 "))

        # 7. check
        rc, out = self._run_cli("check", self.db_path)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "OK")

        # 8. compact
        rc, out = self._run_cli("compact", self.db_path)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "OK")


class TestServeProtocol(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "serve_test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_serve_protocol_conversation(self):
        cmd = [sys.executable, KVDB_BIN, "serve", self.db_path]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        stdin = proc.stdin
        stdout = proc.stdout

        try:
            # 1. Expect READY
            ready_line = stdout.readline()
            self.assertTrue(ready_line.startswith(b"READY keys=0"))

            # 2. PUT
            stdin.write(b"PUT op1 61 31\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"ACK op1\n")

            # 3. GET
            stdin.write(b"GET op2 61\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"VAL op2 31\n")

            # 4. PUT empty value ("-")
            stdin.write(b"PUT op3 62 -\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"ACK op3\n")

            # GET empty value
            stdin.write(b"GET op4 62\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"VAL op4 -\n")

            # 5. DEL
            stdin.write(b"DEL op5 61\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"ACK op5\n")

            stdin.write(b"GET op6 61\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"NOTFOUND op6\n")

            # 6. BATCH
            stdin.write(b"BATCH op7 2\nput 63 33\ndel 62\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"ACK op7\n")

            stdin.write(b"GET op8 63\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"VAL op8 33\n")

            # 7. COMPACT
            stdin.write(b"COMPACT op9\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"ACK op9\n")

            # 8. Pipelined requests (send batch of commands without waiting)
            n_pipe = 40
            for i in range(n_pipe):
                hex_k = f"pipe_{i}".encode("ascii").hex().encode("ascii")
                hex_v = f"val_{i}".encode("ascii").hex().encode("ascii")
                stdin.write(f"PUT p{i} ".encode("ascii") + hex_k + b" " + hex_v + b"\n")
            stdin.flush()

            # Read all 40 acks (they may arrive in any order)
            received_ids = set()
            for _ in range(n_pipe):
                line = stdout.readline()
                parts = line.strip().split(b" ")
                self.assertEqual(parts[0], b"ACK")
                received_ids.add(parts[1].decode("ascii"))
            self.assertEqual(len(received_ids), n_pipe)

            # 9. QUIT
            stdin.write(b"QUIT\n")
            stdin.flush()

            proc.wait(timeout=5)
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
            if proc.stdin:
                proc.stdin.close()
            if proc.stderr:
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
