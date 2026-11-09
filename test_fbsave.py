import codecs
import csv
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from fbsave import CSV_FIELDS, write_outputs


class WriteOutputsTest(unittest.TestCase):
    def test_csv_and_json_preserve_comments_and_manifest_hash(self):
        comment = {
            "index": 1,
            "comment_id": "00123",
            "is_reply": "nein",
            "parent_id": "",
            "parent_author": "",
            "author": "Jörg Öster",
            "profile_url": "https://example.test/joerg",
            "timestamp_raw": "vor 5 Min.",
            "timestamp_utc": "2026-06-10T10:00:00+00:00",
            "timestamp_source": "graphql_exact",
            "text": 'Grüße, "Hallo" 👋\nZweite Zeile\r\n最後の行',
            "attachment_alt": "Bild mit Löwe 🦁",
            "screenshot_file": "comment_001.png",
            "aria_label": "Kommentar von Jörg Öster",
        }
        reply = dict(
            comment, index=2, comment_id="00124", is_reply="ja",
            parent_id=comment["comment_id"], parent_author=comment["author"],
            author="Zoë", profile_url="https://example.test/zoe", text="",
            attachment_alt="", screenshot_file="",
            aria_label="Antwort von Zoë an Jörg Öster",
        )
        rows = [comment, reply]

        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            outdir = Path(directory)
            write_outputs(outdir, rows, {})

            csv_path = outdir / "comments.csv"
            self.assertTrue(csv_path.read_bytes().startswith(codecs.BOM_UTF8))
            with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                self.assertEqual(reader.fieldnames, CSV_FIELDS)
                csv_rows = list(reader)

            json_path = outdir / "comments.json"
            json_bytes = json_path.read_bytes()
            json_text = json_bytes.decode("utf-8")
            json_rows = json.loads(json_text)
            self.assertEqual(json_rows, rows)
            self.assertEqual(csv_rows, [dict(row, index=str(row["index"])) for row in json_rows])
            self.assertIn("Grüße", json_text)
            self.assertIn("👋", json_text)
            self.assertTrue(json_text.startswith("[\n  {\n"))

            manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["files_sha256"]["comments.json"],
                             hashlib.sha256(json_bytes).hexdigest())
            self.assertIn(f"📄 JSON:         {json_path}", output.getvalue())

    def test_empty_capture_writes_empty_json_array(self):
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            outdir = Path(directory)
            write_outputs(outdir, [], {})

            with (outdir / "comments.csv").open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                self.assertEqual(reader.fieldnames, CSV_FIELDS)
                self.assertEqual(list(reader), [])
            self.assertEqual((outdir / "comments.json").read_text(encoding="utf-8"), "[]")
