import codecs
import csv
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from fbsave import CSV_FIELDS, SORT_TARGET_RE, build_comment_tree, sort_title, write_outputs


class CommentSortTest(unittest.TestCase):
    def test_sort_target_matches_the_title_only(self):
        for newest, all_comments, expected in (
            ("Neueste zuerst\nAlle Kommentare anzeigen, die neuesten zuerst.",
             "Alle Kommentare\ufeff\nAlle Kommentare anzeigen, auch potenziellen Spam.",
             "Alle Kommentare"),
            ("Newest\nShow all comments, newest first.",
             "All comments\ufeff\nShow all comments, including potential spam.",
             "All comments"),
        ):
            with self.subTest(title=expected):
                titles = [sort_title(option) for option in (newest, all_comments)]
                self.assertEqual([title for title in titles if SORT_TARGET_RE.fullmatch(title)],
                                 [expected])


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
        nested_reply = dict(
            reply, index=3, comment_id="00125", parent_id=reply["comment_id"],
            parent_author=reply["author"], author="Clara", text="Danke!",
            profile_url="https://example.test/clara",
            aria_label="Antwort von Clara auf Zoës Antwort",
        )
        rows = [comment, reply, nested_reply]

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
            self.assertEqual(json_rows, [dict(comment, replies=[
                dict(reply, replies=[dict(nested_reply, replies=[])]),
            ])])
            json_thread = [json_rows[0], json_rows[0]["replies"][0],
                           json_rows[0]["replies"][0]["replies"][0]]
            self.assertEqual(csv_rows, [
                {field: str(node[field]) for field in CSV_FIELDS}
                for node in json_thread
            ])
            self.assertTrue(all("replies" not in row for row in rows))
            self.assertIn("Grüße", json_text)
            self.assertIn("👋", json_text)
            self.assertTrue(json_text.startswith("[\n  {\n"))

            manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["files_sha256"]["comments.json"],
                             hashlib.sha256(json_bytes).hexdigest())
            self.assertIn(f"📄 JSON:         {json_path}", output.getvalue())

    def test_tree_keeps_replies_with_missing_or_conflicting_parents(self):
        comment = dict(index=1, comment_id="1", is_reply="nein", parent_id="",
                       author="Anna", parent_author="")
        reply = dict(index=2, comment_id="2", is_reply="ja", parent_id="1",
                     author="Ben", parent_author="Anna")
        conflicting = dict(index=3, comment_id="3", is_reply="ja", parent_id="1",
                           author="Clara", parent_author="Ben")
        orphan = dict(index=4, comment_id="4", is_reply="ja", parent_id="9",
                      author="David", parent_author="Eva")

        self.assertEqual(build_comment_tree([reply, comment, conflicting, orphan]), [
            dict(comment, replies=[dict(reply, replies=[])]),
            dict(conflicting, replies=[]),
            dict(orphan, replies=[]),
        ])

    def test_tree_breaks_cyclic_parent_links(self):
        first = dict(index=1, comment_id="1", is_reply="ja", parent_id="2",
                     author="Anna", parent_author="Ben")
        second = dict(index=2, comment_id="2", is_reply="ja", parent_id="1",
                      author="Ben", parent_author="Anna")

        self.assertEqual(build_comment_tree([first, second]), [
            dict(second, replies=[dict(first, replies=[])]),
        ])

    def test_empty_capture_writes_empty_json_array(self):
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            outdir = Path(directory)
            write_outputs(outdir, [], {})

            with (outdir / "comments.csv").open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                self.assertEqual(reader.fieldnames, CSV_FIELDS)
                self.assertEqual(list(reader), [])
            self.assertEqual((outdir / "comments.json").read_text(encoding="utf-8"), "[]")
