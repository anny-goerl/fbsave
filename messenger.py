"""Resumable, DOM-only archive of one explicitly selected Messenger thread.

The journal is the recovery source; JSON/CSV/manifest are replaceable projections.
No Facebook protocol or undocumented GraphQL endpoint is used.
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import platform
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
MONTHS = {name: i + 1 for i, name in enumerate(
    "januar februar märz april mai juni juli august september oktober november dezember".split())}
MONTHS.update({name: i + 1 for i, name in enumerate(
    "january february march april may june july august september october november december".split())})
DE_LABEL = re.compile(
    r"^(?:Um|Am)\s+(?P<date>\d{1,2}\.\s*\w+\s+\d{4}),?\s*"
    r"(?P<clock>\d{1,2}:\d{2}(?::\d{2})?),?\s*(?P<sender>[^:]+):\s*(?P<text>.*)$", re.S | re.I)
EN_LABEL = re.compile(
    r"^(?:At|On)\s+(?P<date>\w+\s+\d{1,2},?\s+\d{4})(?:,| at)?\s*"
    r"(?P<clock>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?),?\s*"
    r"(?P<sender>[^:]+):\s*(?P<text>.*)$", re.S | re.I)
AUX_LABEL = re.compile(r"^(?:Eingabe|Input|Nachricht gesendet|Message sent)(?:,|:|$)", re.I)


class ExportError(Exception):
    """Only fixed, non-sensitive messages may be raised as ExportError."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def chat_url(value):
    """Accept an explicit HTTPS thread only, discard query/fragment tracking."""
    try:
        u = urlsplit(value)
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
            raise ValueError
        if u.hostname in ("www.facebook.com", "facebook.com", "web.facebook.com", "m.facebook.com"):
            prefix, host = "messages/", "www.facebook.com"
        elif u.hostname in ("www.messenger.com", "messenger.com"):
            prefix, host = "", "www.messenger.com"
        else:
            raise ValueError
        match = re.fullmatch(r"/" + prefix + r"(e2ee/)?t/([A-Za-z0-9._-]+)/?", u.path)
        if not match:
            raise ValueError
        return f"https://{host}/{prefix}{match[1] or ''}t/{match[2]}", match[2]
    except ValueError:
        raise ExportError("Bitte eine HTTPS-Chat-URL von facebook.com/messages/(e2ee/)t/ID "
                          "oder messenger.com/(e2ee/)t/ID angeben.") from None


def same_chat(value, expected_id):
    try:
        return chat_url(value)[1] == expected_id
    except ExportError:
        return False


def parse_label(label):
    """Parse explicit German/English dates only; never invent a year/timezone."""
    match = DE_LABEL.match(label) or EN_LABEL.match(label)
    if not match:
        return None
    date, clock = match["date"], match["clock"].strip()
    stamp = None
    try:
        parts = re.sub(r"[.,]", "", date.lower()).split()
        day, month = (int(parts[0]), MONTHS[parts[1]]) if parts[0].isdigit() else (int(parts[1]), MONTHS[parts[0]])
        hm = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?", clock.lower())
        hour, minute, second = int(hm[1]), int(hm[2]), int(hm[3] or 0)
        if hm[4]:
            if not 1 <= hour <= 12:
                raise ValueError
            hour = hour % 12 + (12 if hm[4] == "pm" else 0)
        stamp = datetime(int(parts[2]), month, day, hour, minute, second).isoformat()
    except (ValueError, KeyError, TypeError):
        pass
    return {"sender": match["sender"].strip(), "text": match["text"],
            "date": date, "time": clock, "timestamp_local": stamp,
            "timestamp_raw": date + ", " + clock, "timestamp_source": "aria_label"}


def parse_row(raw):
    labels = [s for s in raw.get("labels", []) if s and not AUX_LABEL.match(s)]
    parsed = next((value for label in labels if (value := parse_label(label))), None)
    if not parsed and raw.get("labels") and not labels and not raw.get("text"):
        return None
    has_metadata = bool(raw.get("message_id") or raw.get("sender") or raw.get("timestamp"))
    if not parsed and not has_metadata:
        # Auxiliary-only rows are accessibility echoes, not separate messages.
        if raw.get("labels") and all(AUX_LABEL.match(s) for s in raw["labels"] if s):
            return None
        if not raw.get("dom_text", "").strip() and not raw.get("attachments"):
            return None
    result = parsed or {"sender": raw.get("sender", ""), "text": raw.get("text") or raw.get("dom_text", "").strip(),
                        "date": None, "time": None, "timestamp_local": None,
                        "timestamp_raw": None, "timestamp_source": "unknown"}
    result.update(message_id=raw.get("message_id", ""), id_source=raw.get("id_source", ""),
                  aria_label=next((s for s in labels if parse_label(s)), labels[0] if labels else ""),
                  timestamp_utc=None, parse_status="parsed" if parsed else "unparsed",
                  attachments=[{k: a.get(k, "") for k in ("url", "kind", "name", "alt")}
                               for a in raw.get("attachments", [])])
    # Explicit DOM text is preferred over a possibly truncated accessibility echo.
    if raw.get("text"):
        result["text"] = raw["text"]
    if raw.get("sender"):
        result["sender"] = raw["sender"]
    stamp = str(raw.get("timestamp") or "")
    if stamp:
        result["timestamp_metadata"] = stamp
        try:
            if re.fullmatch(r"\d{10}(?:\d{3})?", stamp):
                dt = datetime.fromtimestamp(int(stamp) / (1000 if len(stamp) == 13 else 1), timezone.utc)
            else:
                dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            result["timestamp_source"] = "dom_metadata"
            if result["sender"]:
                result["parse_status"] = "parsed"
            result["timestamp_local"] = dt.isoformat()
            result["date"], result["time"] = dt.date().isoformat(), dt.time().isoformat()
            if dt.tzinfo is not None:
                result["timestamp_utc"] = dt.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return result


def atomic_write(path, data):
    """Replace one projection atomically; the fsynced journal remains authoritative."""
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Archive:
    def __init__(self, directory, url, resume=False):
        self.directory = Path(directory)
        self.url, self.chat_id = chat_url(url)
        self.messages = {}
        self.generation = 0
        self.journal = self.directory / "messages.jsonl"
        self.checkpoint = self.directory / "checkpoint.json"
        self.run = {"started_utc": utc_now(), "status": "running", "rounds": 0,
                    "top_stable": False, "bottom_stable": False}
        self.runs = []
        self.recovered_tail = False
        self.media_attempts = set()
        self._lock = None
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            # Advisory lock is released even on SIGKILL; no stale lock blocks resume.
            import fcntl
            self._lock = (self.directory / ".export.lock").open("a")
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError):
            self.close()
            raise ExportError("Der Ausgabeordner ist gesperrt oder Dateisperren werden nicht unterstützt.") from None
        try:
            contents = [p for p in self.directory.iterdir() if p.name != ".export.lock"]
            if contents:
                if not resume:
                    raise ExportError("Ausgabeordner ist nicht leer. Für diesen Export --resume verwenden.")
                try:
                    saved = json.loads(self.checkpoint.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise ExportError("Kein gültiger Checkpoint im Ausgabeordner; bitte einen neuen Ordner wählen.") from None
                if saved.get("schema_version") != SCHEMA_VERSION or saved.get("chat_id") != self.chat_id:
                    raise ExportError("Checkpoint gehört zu einem anderen Chat oder einer anderen Formatversion.")
                self.runs = saved.get("runs", [])
                if self.runs and self.runs[-1].get("status") == "running":
                    self.runs[-1]["status"] = "unclean_stop"
                if self.journal.exists():
                    self._recover()
                elif (self.directory / "messages.json").exists():
                    raise ExportError("Das Nachrichtenjournal fehlt; Wiederaufnahme ohne Journal ist nicht sicher.")
            elif resume:
                raise ExportError("Für --resume ist noch kein Archiv vorhanden.")
            self.runs.append(self.run)
            self.save_checkpoint()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def _recover(self):
        # Only an incomplete final line may be discarded after a process/power loss.
        with self.journal.open("r+b") as stream:
            valid = 0
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    stream.truncate(valid)
                    self.recovered_tail = True
                    break
                try:
                    message = json.loads(line)
                    self.messages[message["key"]] = message
                except (ValueError, KeyError, TypeError):
                    raise ExportError("Das Nachrichtenjournal ist beschädigt; keine automatische Überschreibung.") from None
                valid = stream.tell()

    def save_checkpoint(self):
        atomic_write(self.checkpoint, encoded({"schema_version": SCHEMA_VERSION, "chat_id": self.chat_id,
            "chat_url": self.url, "runs": self.runs, "recovered_incomplete_tail": self.recovered_tail}))

    def persist(self, messages):
        if not messages:
            return
        with self.journal.open("ab") as stream:
            for message in messages:
                stream.write(encoded(message) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        for message in messages:
            self.messages[message["key"]] = message
        self.generation += 1

    def observe(self, rows):
        occurrences, changes = {}, []
        for raw in rows:
            message = parse_row(raw)
            if message is None:
                continue
            if message["message_id"]:
                key = "id:" + message["message_id"]
                message["deduplication"] = "message_id"
            else:
                # Signed URLs are deliberately excluded: they expire/change on reload.
                identity = {k: message.get(k) for k in ("sender", "timestamp_raw", "timestamp_metadata", "text")}
                identity["attachments"] = [{k: a.get(k) for k in ("kind", "name", "alt")} for a in message["attachments"]]
                fingerprint = digest(encoded(identity))
                occurrences[fingerprint] = occurrences.get(fingerprint, 0) + 1
                key = f"fallback:{fingerprint}:{occurrences[fingerprint]}"
                message["deduplication"] = "content_sha256_and_snapshot_occurrence"
            message["key"] = key
            previous = self.messages.get(key)
            message["first_seen_utc"] = previous["first_seen_utc"] if previous else utc_now()
            if previous:
                # A different URL may be a different attachment, not just a refreshed
                # signature. Never assign an old file to a new source by position.
                old_files = {a.get("url"): a for a in previous.get("attachments", [])}
                for attachment in message["attachments"]:
                    old = old_files.get(attachment.get("url"))
                    if old:
                        for field in ("local_path", "sha256", "size", "status", "error"):
                            if field in old:
                                attachment[field] = old[field]
                message["revisions"] = previous.get("revisions", [])
                if any(previous.get(k) != message.get(k) for k in ("sender", "text", "timestamp_local", "attachments")):
                    message["revisions"] = message["revisions"] + [
                        {k: previous.get(k) for k in ("sender", "text", "timestamp_local", "aria_label", "attachments")}]
            else:
                message["revisions"] = []
            if previous != message:
                changes.append(message)
        self.persist(changes)
        return len(changes)

    def outputs(self):
        self.run["finished_utc"] = utc_now()
        self.save_checkpoint()
        rows = list(self.messages.values())
        # Known local/UTC timestamps sort chronologically; ties retain observation order.
        rows.sort(key=lambda m: (m.get("timestamp_utc") or m.get("timestamp_local") or "9999"))
        atomic_write(self.directory / "messages.json", json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"))
        fields = ["key", "message_id", "id_source", "deduplication", "sender", "date", "time",
                  "timestamp_raw", "timestamp_local", "timestamp_utc", "timestamp_source", "text",
                  "parse_status", "aria_label", "attachments", "first_seen_utc"]
        table = io.StringIO(newline="")
        writer = csv.DictWriter(table, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            value = dict(row, attachments=json.dumps(row["attachments"], ensure_ascii=False))
            # Spreadsheet applications otherwise interpret attacker-controlled formulas.
            for key, cell in value.items():
                if isinstance(cell, str) and cell.lstrip().startswith(("=", "+", "-", "@")):
                    value[key] = "'" + cell
            writer.writerow(value)
        atomic_write(self.directory / "messages.csv", table.getvalue().encode("utf-8-sig"))
        files = {}
        for name in ("messages.jsonl", "checkpoint.json", "messages.json", "messages.csv"):
            path = self.directory / name
            if path.exists():
                files[name] = file_hash(path)
        # Include earlier attachment versions and files written just before a crash.
        for path in (self.directory / "media").glob("*.bin"):
            if re.fullmatch(r"[0-9a-f]{64}\.bin", path.name) and path.is_file():
                files[path.relative_to(self.directory).as_posix()] = file_hash(path)
        media = [a for m in rows for a in m["attachments"]]
        manifest = {"schema_version": SCHEMA_VERSION, "chat_url": self.url, "chat_id": self.chat_id,
            "generated_utc": utc_now(), "runs": self.runs, "message_count": len(rows),
            "fallback_count": sum(m["deduplication"] != "message_id" for m in rows),
            "unparsed_count": sum(m["parse_status"] == "unparsed" for m in rows),
            "attachments_saved": sum(a.get("status") == "saved" for a in media),
            "attachments_missing": sum(a.get("status") != "saved" for a in media),
            "completeness": "unverified", "files": files,
            "limitations": ["DOM-only; no authoritative message count or history cursor.",
                "Stable scroll boundaries are heuristic, not proof of completeness.",
                "Fallback IDs may merge identical messages observed in separate virtual windows.",
                "Opening the chat may cause automatic read receipts.",
                "ARIA dates have no verified timezone; timestamp_utc stays null unless explicit."]}
        atomic_write(self.directory / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return {"sha256": h.hexdigest(), "size": path.stat().st_size}


def media_url_allowed(value):
    try:
        u = urlsplit(value)
        host = u.hostname or ""
        return (u.scheme == "https" and not u.username and not u.password and u.port in (None, 443)
                and any(host == domain or host.endswith("." + domain)
                        for domain in ("fbcdn.net", "fbsbx.com")))
    except ValueError:
        return False


def save_media(archive, context, page, max_bytes):
    """Only GET direct Meta CDN candidates already observed inside this chat.

    No link clicks or external link previews. Redirect targets are validated anew.
    blob: support is restricted to the current page's origin, and size checked in JS.
    """
    for message in list(archive.messages.values()):
        updated = json.loads(json.dumps(message))
        changed = False
        for attachment in updated["attachments"]:
            path = archive.directory / attachment.get("local_path", "_missing")
            if attachment.get("status") == "saved" and path.is_file() and file_hash(path)["sha256"] == attachment.get("sha256"):
                continue
            url, body, error = attachment.get("url", ""), None, None
            attempt = (message["key"], url)
            if attempt in archive.media_attempts:
                continue
            archive.media_attempts.add(attempt)
            try:
                if url.startswith("blob:"):
                    value = page.evaluate("""async ({url, limit}) => {
                      if (!url.startsWith('blob:' + location.origin + '/')) return null;
                      const r = await fetch(url); const b = await r.blob();
                      if (b.size > limit) return null;
                      const bytes = new Uint8Array(await b.arrayBuffer());
                      let s = ''; for (let i=0; i<bytes.length; i+=8192)
                        s += String.fromCharCode(...bytes.subarray(i, i+8192));
                      return btoa(s);
                    }""", {"url": url, "limit": max_bytes})
                    if value is not None:
                        body = base64.b64decode(value)
                    else:
                        error = "blob_unavailable_or_too_large"
                elif media_url_allowed(url):
                    for _ in range(6):
                        if not media_url_allowed(url):
                            error = "redirect_not_allowed"
                            break
                        response = context.request.get(url, timeout=15000, max_redirects=0)
                        try:
                            if response.status in (301, 302, 303, 307, 308):
                                from urllib.parse import urljoin
                                url = urljoin(url, response.headers.get("location", ""))
                                continue
                            if response.status != 200:
                                error = "http_error"
                            elif int(response.headers.get("content-length", "0")) > max_bytes:
                                error = "too_large"
                            elif "text/html" in response.headers.get("content-type", "").lower():
                                error = "unexpected_html"
                            else:
                                body = response.body()
                            break
                        finally:
                            response.dispose()
                    else:
                        error = "too_many_redirects"
                else:
                    error = "source_not_allowed"
                if body is not None and len(body) > max_bytes:
                    body, error = None, "too_large"
            except Exception:
                # Playwright exceptions can contain signed URLs/headers: never log them.
                error = "download_failed"
            if body is not None:
                content_hash = digest(body)
                relative = "media/" + content_hash + ".bin"
                target = archive.directory / relative
                target.parent.mkdir(exist_ok=True, mode=0o700)
                atomic_write(target, body)
                attachment.update(status="saved", local_path=relative, sha256=content_hash, size=len(body))
                attachment.pop("error", None)
            else:
                attachment.update(status="unavailable", error=error or "unavailable")
                for field in ("local_path", "sha256", "size"):
                    attachment.pop(field, None)
            changed = True
        if changed:
            archive.persist([updated])


def collect(page, archive):
    if not same_chat(page.url, archive.chat_id):
        raise ExportError("Chat nicht erreichbar: Session, Zugriffsrechte oder Zugangskontrolle prüfen.")
    state = page.evaluate("() => window.__fbsave?.poll()")
    if not state:
        raise ExportError("Nachrichtensammler nicht verfügbar; bisherige Daten bleiben erhalten.")
    if state.get("blocked"):
        raise ExportError("Login-, PIN- oder Captcha-Sperre erkannt. Zugang manuell prüfen; der Export wird beendet.")
    if state["ambiguous"]:
        raise ExportError("Mehrere Nachrichtenlisten gefunden; eindeutige Chat-Zuordnung nicht möglich.")
    last = 0
    for batch in state["batches"]:
        archive.observe(batch["rows"])
        last = batch["sequence"]
    if last:
        page.evaluate("n => window.__fbsave.ack(n)", last)
    archive.run["removed_rows_observed"] = state["removed_rows"]
    if state["overflow"]:
        raise ExportError("Nachrichtenpuffer übergelaufen; Lauf ist unvollständig. Mit --resume wiederholen.")
    return state


def traverse(page, archive, args, media=None):
    """Sweep upwards to a quiet boundary, then downwards to cover both directions."""
    for direction, boundary in (("up", "at_top"), ("down", "at_bottom")):
        quiet_since, previous = None, None
        while archive.run["rounds"] < args.max_rounds:
            state = collect(page, archive)
            if not state["available"]:
                raise ExportError("Nachrichtenliste nicht mehr zugänglich; Session oder Zugriff prüfen.")
            if media:
                media()
            signature = (archive.generation, state["scroll"].get("top"), state["scroll"].get("height"))
            import time
            now = time.monotonic()
            if state["scroll"].get(boundary) and not state["loading"] and signature == previous:
                if quiet_since is None:
                    quiet_since = now
                if now - quiet_since >= args.idle_seconds:
                    archive.run["top_stable" if direction == "up" else "bottom_stable"] = True
                    break
            else:
                quiet_since = None
            previous = signature
            # Moving again while the virtual list is loading can skip entire windows.
            if not state["loading"]:
                page.evaluate("d => window.__fbsave.scroll(d)", direction)
            archive.run["rounds"] += 1
            archive.save_checkpoint()
            # Short polls persist transient messages during slow loading, rather than
            # reading only the final DOM after a long sleep.
            for _ in range(4):
                page.wait_for_timeout(args.poll_ms)
                collect(page, archive)
        else:
            archive.run["status"] = "round_limit"
            return 2
    archive.run["status"] = "boundaries_stable"
    if not archive.messages:
        archive.run["status"] = "empty_unverified"
        return 2
    return 0


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Wert muss größer als 0 sein.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Archiviert genau einen zugänglichen Messenger-Chat (DOM, JSON/CSV/Hashes).")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, help="Eigener Ausgabeordner für diesen Chat")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--resume", action="store_true", help="Archiv desselben Chats fortsetzen")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--max-rounds", type=positive_int, default=2000)
    parser.add_argument("--idle-seconds", type=positive_int, default=15, help="Ruhezeit je Scrollgrenze (Heuristik)")
    parser.add_argument("--poll-ms", type=positive_int, default=250)
    parser.add_argument("--no-media", action="store_true", help="Nur Text und Anhang-Metadaten sichern")
    parser.add_argument("--max-media-mb", type=positive_int, default=50)
    # Deliberately never produces DOM dumps, screenshots, console/network logs.
    parser.add_argument("--debug", action="store_true", help="Nur zusätzliche anonyme Zähler ausgeben")
    args = parser.parse_args(argv)
    archive, page, browser = None, None, None
    exit_code = 1
    previous_handler = signal.getsignal(signal.SIGTERM)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        url, chat_id = chat_url(args.url)
        if not Path(args.state).is_file():
            raise ExportError("Session-Datei fehlt. Mit save_login.py manuell anmelden und state.json speichern.")
        # Session data is passed to Playwright, never copied to an archive or logged.
        archive = Archive(args.output, url, args.resume)
        archive.run["options"] = {key: getattr(args, key) for key in
            ("max_rounds", "idle_seconds", "poll_ms", "no_media", "max_media_mb", "headless")}
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=args.headless)
                context = browser.new_context(storage_state=args.state, locale="de-DE", service_workers="block")
                script = Path(__file__).with_name("messenger_dom.js").read_text(encoding="utf-8")
                from importlib.metadata import version
                archive.run["runtime"] = {"python": platform.python_version(), "playwright": version("playwright"),
                    "browser": browser.version, "locale": "de-DE", "collector_sha256": digest(script.encode("utf-8")),
                    "exporter_sha256": file_hash(Path(__file__))["sha256"]}
                context.add_init_script(script=script.replace("__CHAT_ID__", json.dumps(chat_id)))
                page = context.new_page()
                def guard(route):
                    request = route.request
                    if request.is_navigation_request() and request.frame == page.main_frame and not same_chat(request.url, chat_id):
                        route.abort()
                    else:
                        route.fallback()
                page.route("**/*", guard)
                print("Messenger-Chat wird geöffnet. Automatische Lesebestätigungen sind möglich.", flush=True)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Wait while draining the collector; do not wait for networkidle on Messenger.
                for _ in range(120):
                    state = collect(page, archive)
                    if state["available"]:
                        break
                    page.wait_for_timeout(250)
                else:
                    raise ExportError("Keine zugängliche Nachrichtenliste gefunden. Session, Chat-Zugriff, "
                                      "PIN/E2EE-Freigabe oder Zugangskontrolle manuell prüfen; nichts wird umgangen.")
                media = None if args.no_media else lambda: save_media(archive, context, page, args.max_media_mb * 1024 * 1024)
                exit_code = traverse(page, archive, args, media)
                collect(page, archive)
                if media:
                    media()
            finally:
                # Drain before closing, including on Ctrl-C and partial media failure.
                if page is not None:
                    try:
                        collect(page, archive)
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except KeyboardInterrupt:
        exit_code = 130
        if archive:
            archive.run["status"] = "interrupted"
        print("Unterbrochen; beobachtete Nachrichten werden gesichert. Fortsetzen mit --resume.")
    except ExportError as exc:
        exit_code = 1
        if archive:
            archive.run["status"] = "error"
        print(str(exc), file=sys.stderr)
    except Exception:
        exit_code = 1
        if archive:
            archive.run["status"] = "error"
        print("Export fehlgeschlagen. Session/Zugriff, Playwright und Chromium prüfen "
              "(python -m playwright install chromium). Vorhandene Daten bleiben erhalten.", file=sys.stderr)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        if archive:
            try:
                archive.outputs()
                print(f"{len(archive.messages)} Nachrichten gesichert; Vollständigkeit nicht verifiziert.")
                if args.debug:
                    print(f"Laderunden: {archive.run['rounds']}; Status: {archive.run['status']}")
            except Exception:
                print("Ausgabedateien konnten nicht vollständig geschrieben werden. "
                      "Das Journal für die Wiederaufnahme aufbewahren.", file=sys.stderr)
                exit_code = 1
            finally:
                archive.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
