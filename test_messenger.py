"""Synthetic data only: no real session, Facebook access or private messages."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import messenger as m

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).parent
URL = "https://www.facebook.com/messages/t/123"


def raw(text="Hallo", message_id="m1", **kwargs):
    return dict(message_id=message_id, id_source="data-message-id" if message_id else "",
                labels=[f"Um 16. Oktober 2026, 11:53, Du: {text}"], attachments=[], **kwargs)


class ParserTest(unittest.TestCase):
    def test_german_english_multiline_colons_and_unknown_timezone(self):
        value = m.parse_row(raw("A: B\nC"))
        self.assertEqual(value["sender"], "Du")
        self.assertEqual(value["text"], "A: B\nC")
        self.assertEqual(value["timestamp_local"], "2026-10-16T11:53:00")
        self.assertIsNone(value["timestamp_utc"])
        value = m.parse_label("At October 16, 2026, 11:53 PM, Alex: Hello")
        self.assertEqual(value["timestamp_local"], "2026-10-16T23:53:00")
        value = m.parse_label("Um 32. Oktober 2026, 11:53, Alex: kaputtes Datum")
        self.assertIsNone(value["timestamp_local"])

    def test_explicit_metadata_has_utc_and_auxiliary_rows_are_ignored(self):
        value = m.parse_row(raw(timestamp="2026-10-16T11:53:00+02:00"))
        self.assertEqual(value["timestamp_utc"], "2026-10-16T09:53:00+00:00")
        self.assertIsNone(m.parse_row({"message_id": "echo", "labels": ["Eingabe, Nachricht gesendet: Hallo"]}))
        value = m.parse_row({"message_id": "x", "labels": ["Unbekannt"], "dom_text": "Behalten"})
        self.assertEqual(value["parse_status"], "unparsed")
        self.assertEqual(value["text"], "Behalten")

    def test_urls_reject_other_sites_credentials_and_non_thread_paths(self):
        for value in ("http://www.facebook.com/messages/t/123", "https://evil.test/t/123",
                      "https://www.facebook.com/messages/", "https://user:secret@www.facebook.com/messages/t/123",
                      "https://www.facebook.com:444/messages/t/123", "https://www.facebook.com/messages/t/../other"):
            with self.subTest(value=value), self.assertRaises(m.ExportError):
                m.chat_url(value)
        self.assertEqual(m.chat_url(URL + "?tracking=x#fragment"), (URL, "123"))
        self.assertTrue(m.same_chat("https://www.messenger.com/e2ee/t/123", "123"))
        self.assertFalse(m.same_chat(URL + "4", "123"))

    def test_media_allowlist(self):
        for value in ("https://fbcdn.net.evil.test/x", "https://127.0.0.1/x", "file:///etc/passwd",
                      "https://www.facebook.com/messages/t/999", "https://l.facebook.com/redirect",
                      "https://user:pass@cdn.fbcdn.net/x"):
            self.assertFalse(m.media_url_allowed(value))
        self.assertTrue(m.media_url_allowed("https://scontent.xx.fbcdn.net/x"))


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "export"
        self.archive = m.Archive(self.directory, URL)

    def tearDown(self):
        self.archive.close()
        self.temporary.cleanup()

    def test_ids_edits_and_fallback_snapshot_occurrences(self):
        self.archive.observe([raw(), raw("Hallo", "m2"), raw("identisch", ""), raw("identisch", "")])
        self.archive.observe([raw(), raw("Hallo", "m2"), raw("identisch", ""), raw("identisch", "")])
        self.assertEqual(len(self.archive.messages), 4)
        self.archive.observe([raw("Bearbeitet")])
        self.assertEqual(len(self.archive.messages), 4)
        self.assertEqual(self.archive.messages["id:m1"]["revisions"][0]["text"], "Hallo")

    def test_resume_after_hard_stop_replays_journal_not_checkpoint_count(self):
        self.archive.observe([raw()])
        self.archive.close()
        with (self.directory / "messages.jsonl").open("ab") as stream:
            stream.write(b'{"partial":')
        resumed = m.Archive(self.directory, URL, resume=True)
        try:
            self.assertTrue(resumed.recovered_tail)
            self.assertEqual(len(resumed.messages), 1)
            resumed.observe([raw(), raw("Neu", "m2")])
            resumed.outputs()
            manifest = json.loads((self.directory / "manifest.json").read_text())
            for name, value in manifest["files"].items():
                self.assertEqual(value, m.file_hash(self.directory / name))
            self.assertEqual(manifest["completeness"], "unverified")
        finally:
            resumed.close()

    def test_wrong_chat_nonempty_and_concurrent_writer_are_rejected(self):
        with self.assertRaises(m.ExportError):
            m.Archive(self.directory, URL, resume=True)
        self.archive.close()
        with self.assertRaises(m.ExportError):
            m.Archive(self.directory, URL)
        with self.assertRaises(m.ExportError):
            m.Archive(self.directory, URL + "4", resume=True)

    def test_corrupt_complete_journal_line_is_not_discarded(self):
        self.archive.observe([raw()])
        self.archive.close()
        with (self.directory / "messages.jsonl").open("ab") as stream:
            stream.write(b'{invalid}\n')
        before = (self.directory / "messages.jsonl").read_bytes()
        with self.assertRaises(m.ExportError):
            m.Archive(self.directory, URL, resume=True)
        self.assertEqual(before, (self.directory / "messages.jsonl").read_bytes())

    def test_csv_escapes_formulas_json_preserves_original(self):
        self.archive.observe([raw("=1+1")])
        self.archive.outputs()
        self.assertEqual(json.loads((self.directory / "messages.json").read_text())[0]["text"], "=1+1")
        self.assertIn("'=1+1", (self.directory / "messages.csv").read_text(encoding="utf-8-sig"))

    def test_buffer_overflow_is_an_error_after_saving_available_observations(self):
        page = mock.Mock(url=URL)
        page.evaluate.side_effect = [{"batches": [{"sequence": 1, "rows": [raw()]}],
            "ambiguous": False, "overflow": True, "removed_rows": 0}, None]
        with self.assertRaises(m.ExportError):
            m.collect(page, self.archive)
        self.assertIn("id:m1", self.archive.messages)
        self.assertTrue(self.archive.journal.exists())

    def test_failed_persistence_does_not_acknowledge_browser_queue(self):
        page = mock.Mock(url=URL)
        page.evaluate.return_value = {"batches": [{"sequence": 1, "rows": [raw()]}], "ambiguous": False}
        with mock.patch.object(self.archive, "persist", side_effect=OSError), self.assertRaises(OSError):
            m.collect(page, self.archive)
        self.assertEqual(page.evaluate.call_count, 1)

    def test_media_download_retry_and_hashes(self):
        row = raw()
        row["attachments"] = [{"kind": "img", "url": "https://cdn.fbcdn.net/test"}]
        self.archive.observe([row])
        response = mock.Mock(status=200, headers={"content-type": "image/png"})
        response.body.return_value = b"synthetic image"
        context = mock.Mock()
        context.request.get.return_value = response
        m.save_media(self.archive, context, mock.Mock(), 1024)
        self.archive.observe([row])
        m.save_media(self.archive, context, mock.Mock(), 1024)
        self.assertEqual(context.request.get.call_count, 1)
        attachment = self.archive.messages["id:m1"]["attachments"][0]
        self.assertEqual(attachment["status"], "saved")
        self.assertEqual((self.directory / attachment["local_path"]).read_bytes(), b"synthetic image")
        self.archive.outputs()
        self.assertIn(attachment["local_path"], json.loads((self.directory / "manifest.json").read_text())["files"])

    def test_redirect_and_oversize_do_not_escape_allowlist(self):
        row = raw()
        row["attachments"] = [{"kind": "a", "url": "https://cdn.fbcdn.net/test"}]
        self.archive.observe([row])
        context = mock.Mock()
        context.request.get.return_value = mock.Mock(status=302, headers={"location": "https://evil.test/file"})
        m.save_media(self.archive, context, mock.Mock(), 1024)
        self.assertEqual(context.request.get.call_count, 1)
        self.assertEqual(self.archive.messages["id:m1"]["attachments"][0]["error"], "redirect_not_allowed")
        row["attachments"][0]["url"] += "2"
        self.archive.observe([row])
        response = mock.Mock(status=200, headers={"content-length": "2048"})
        context.request.get.return_value = response
        m.save_media(self.archive, context, mock.Mock(), 1024)
        response.body.assert_not_called()
        self.assertEqual(self.archive.messages["id:m1"]["attachments"][0]["error"], "too_large")

    def test_changed_media_source_never_reuses_old_file_by_position(self):
        row = raw()
        row["attachments"] = [{"kind": "img", "url": "https://cdn.fbcdn.net/old"}]
        self.archive.observe([row])
        response = mock.Mock(status=200, headers={"content-type": "image/png"})
        response.body.return_value = b"old image"
        context = mock.Mock()
        context.request.get.return_value = response
        m.save_media(self.archive, context, mock.Mock(), 1024)
        old_path = self.archive.messages["id:m1"]["attachments"][0]["local_path"]
        row["attachments"][0]["url"] = "https://cdn.fbcdn.net/new"
        self.archive.observe([row])
        self.assertNotIn("local_path", self.archive.messages["id:m1"]["attachments"][0])
        response.body.return_value = b"new image"
        m.save_media(self.archive, context, mock.Mock(), 1024)
        self.assertEqual(context.request.get.call_count, 2)
        self.assertEqual(self.archive.messages["id:m1"]["revisions"][-1]["attachments"][0]["local_path"], old_path)
        self.archive.outputs()
        files = json.loads((self.directory / "manifest.json").read_text())["files"]
        self.assertEqual(len([p for p in files if p.startswith("media/")]), 2)


@unittest.skipUnless(sync_playwright, "Playwright nicht installiert; Browser-Fixtures übersprungen")
class BrowserFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        # If Playwright is installed, missing Chromium is a test failure (also in CI).
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.add_init_script(script=(ROOT / "messenger_dom.js").read_text().replace("__CHAT_ID__", '"123"'))
        self.page = self.context.new_page()
        self.temporary = tempfile.TemporaryDirectory()
        self.archive = m.Archive(Path(self.temporary.name) / "archive", URL)

    def tearDown(self):
        self.context.close()
        self.archive.close()
        self.temporary.cleanup()

    def load(self, fixture="messenger.html", url=URL):
        body = (ROOT / "tests" / "fixtures" / fixture).read_text()
        # Every request is handled locally. No Facebook or CDN access.
        self.context.route("**/*", lambda route: route.fulfill(body=body, content_type="text/html")
                           if route.request.is_navigation_request() else route.abort())
        self.page.goto(url)

    def test_real_dom_ancestor_aria_aux_labels_and_media(self):
        self.load()
        m.collect(self.page, self.archive)
        self.assertEqual(len(self.archive.messages), 6)
        self.assertEqual(self.archive.messages["id:m1"]["text"], "Hallo: Welt")
        self.assertNotIn("id:m1-echo", self.archive.messages)
        self.assertNotIn("id:sidebar-secret", self.archive.messages)
        self.assertEqual(len(self.archive.messages["id:m3"]["attachments"]), 2)
        self.assertEqual(self.page.evaluate("window.actions"), 0)

    def test_transient_rows_are_observed_after_removal(self):
        self.load()
        self.page.evaluate("""() => {
          const root = document.querySelector('[data-scope="messages_table"]');
          const row = document.createElement('div');
          row.setAttribute('role', 'row'); row.dataset.messageId='transient';
          row.setAttribute('aria-label', 'Um 16. Oktober 2026, 11:59, Alex: kurz sichtbar');
          root.append(row); setTimeout(() => row.remove(), 10);
        }""")
        self.page.wait_for_timeout(50)
        state = m.collect(self.page, self.archive)
        self.assertIn("id:transient", self.archive.messages)
        self.assertGreater(state["removed_rows"], 0)
        self.assertEqual(self.page.locator('[data-message-id="transient"]').count(), 0)

    def test_virtual_list_both_directions_with_delayed_replacement(self):
        self.load("virtual_messenger.html")
        self.page.wait_for_timeout(100)
        args = argparse.Namespace(max_rounds=100, idle_seconds=0.15, poll_ms=25)
        code = m.traverse(self.page, self.archive, args)
        self.assertEqual(code, 0)
        self.assertEqual(set(self.archive.messages), {f"id:virtual-{i}" for i in range(12)})
        self.assertLess(self.page.locator('[role="row"]').count(), 12)
        self.assertTrue(self.archive.run["top_stable"])
        self.assertTrue(self.archive.run["bottom_stable"])
        self.assertGreater(self.archive.run["removed_rows_observed"], 0)

    def test_loading_on_table_blocks_premature_boundary_detection(self):
        self.load("virtual_messenger.html")
        self.page.evaluate("window.delay = 450; document.querySelector('[data-scope]').setAttribute('aria-busy', 'true')")
        self.assertTrue(m.collect(self.page, self.archive)["loading"])
        args = argparse.Namespace(max_rounds=100, idle_seconds=0.15, poll_ms=25)
        self.assertEqual(m.traverse(self.page, self.archive, args), 0)
        self.assertEqual(len(self.archive.messages), 12)

    def test_column_reverse_scroll_boundaries(self):
        self.load()
        self.page.evaluate("""() => {
          const root = document.querySelector('[data-scope]');
          root.parentElement.style.cssText = 'height:100px;overflow-y:auto;display:flex;flex-direction:column-reverse';
          root.style.cssText = 'height:600px;flex-shrink:0';
        }""")
        self.assertTrue(self.page.evaluate("window.__fbsave.poll().scroll.at_bottom"))
        self.assertFalse(self.page.evaluate("window.__fbsave.poll().scroll.at_top"))
        for _ in range(10):
            self.page.evaluate("window.__fbsave.scroll('up')")
        self.assertTrue(self.page.evaluate("window.__fbsave.poll().scroll.at_top"))
        self.assertLess(self.page.evaluate("window.__fbsave.poll().scroll.top"), 0)

    def test_blob_media_is_saved_without_external_requests(self):
        self.load()
        url = self.page.evaluate("URL.createObjectURL(new Blob(['synthetic blob'], {type:'text/plain'}))")
        row = raw()
        row["attachments"] = [{"kind": "a", "url": url}]
        self.archive.observe([row])
        m.save_media(self.archive, self.context, self.page, 1024)
        attachment = self.archive.messages["id:m1"]["attachments"][0]
        self.assertEqual(attachment["status"], "saved")
        self.assertEqual((self.archive.directory / attachment["local_path"]).read_bytes(), b"synthetic blob")

    def test_round_limit_is_partial_and_resume_revisits_history(self):
        self.load("virtual_messenger.html")
        args = argparse.Namespace(max_rounds=1, idle_seconds=1, poll_ms=25)
        self.assertEqual(m.traverse(self.page, self.archive, args), 2)
        self.archive.outputs()
        self.archive.close()
        self.archive = m.Archive(self.archive.directory, URL, resume=True)
        args.max_rounds = 100
        args.idle_seconds = 0.15
        self.assertEqual(m.traverse(self.page, self.archive, args), 0)
        self.assertEqual(len(self.archive.messages), 12)

    def test_other_chat_and_multiple_lists_are_rejected(self):
        self.load(url=URL + "4")
        self.assertFalse(self.page.evaluate("window.__fbsave.poll().available"))
        with self.assertRaises(m.ExportError):
            m.collect(self.page, self.archive)
        self.page.goto(URL)
        self.page.evaluate("document.body.append(document.querySelector('[data-scope]').cloneNode(true))")
        with self.assertRaises(m.ExportError):
            m.collect(self.page, self.archive)

    def test_access_gate_blocks_collection_even_with_messages_in_dom(self):
        self.load()
        self.page.evaluate("""() => {
          const pin = document.createElement('input'); pin.type='password';
          document.body.append(pin);
        }""")
        with self.assertRaisesRegex(m.ExportError, "Sperre erkannt"):
            m.collect(self.page, self.archive)
        self.assertFalse(self.archive.messages)

    def test_interrupt_main_saves_and_does_not_log_private_content(self):
        self.load()
        # Exercise main's lifecycle against a real isolated browser with local routes.
        self.archive.close()
        output = Path(self.temporary.name) / "interrupted"
        state = Path(self.temporary.name) / "synthetic-state.json"
        state.write_text('{"cookies":[],"origins":[]}')
        original_new_context = self.browser.new_context
        def new_context(**kwargs):
            context = original_new_context(**kwargs)
            body = (ROOT / "tests/fixtures/messenger.html").read_text()
            context.route("**/*", lambda route: route.fulfill(body=body, content_type="text/html"))
            return context
        fake_pw = mock.Mock()
        fake_browser = mock.Mock()
        fake_browser.version = "synthetic"
        fake_browser.new_context.side_effect = new_context
        fake_pw.chromium.launch.return_value = fake_browser
        manager = mock.MagicMock()
        manager.__enter__.return_value = fake_pw
        def interrupted(page, archive, args, media):
            m.collect(page, archive)
            raise KeyboardInterrupt
        log = io.StringIO()
        with mock.patch("playwright.sync_api.sync_playwright", return_value=manager), \
                mock.patch.object(m, "traverse", side_effect=interrupted), \
                contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            code = m.main(["--url", URL, "--output", str(output), "--state", str(state), "--no-media", "--debug"])
        self.assertEqual(code, 130)
        self.assertTrue(json.loads((output / "messages.json").read_text()))
        self.assertEqual(json.loads((output / "manifest.json").read_text())["runs"][-1]["status"], "interrupted")
        self.assertNotIn("Hallo", log.getvalue())
        self.assertNotIn("cookies", log.getvalue())


class CLITest(unittest.TestCase):
    def test_missing_session_is_clear_and_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "archive"
            log = io.StringIO()
            with contextlib.redirect_stderr(log):
                self.assertEqual(m.main(["--url", URL, "--output", str(output), "--state", temporary + "/missing"]), 1)
            self.assertIn("Session-Datei fehlt", log.getvalue())
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
