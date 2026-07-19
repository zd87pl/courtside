"""courtside-ui - a local SaaS-style web app over the courtside pipeline.

Serves a dashboard on 127.0.0.1: sessions overview, an analyze-from-browser
flow with live progress, and a rich per-session view (timeline, flagged
moments, coaching report). The "backend" is this laptop - the app binds to
localhost only and uploads nothing, which is the product story.

stdlib-only by design (http.server + threads + subprocess): no new deps, no
frameworks, works wherever the core package installs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config
from .fetch import is_url, video_slug, ytdlp_available
from .webui import render_dashboard, render_run_page, render_session

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


# ---------------- session discovery ----------------

@dataclass
class SessionRef:
    sid: str
    dir: Path
    doc: dict

    @property
    def title(self) -> str:
        return self.doc.get("video") or self.dir.name


def _sid_for(path: Path) -> str:
    return hashlib.md5(str(path.resolve()).encode()).hexdigest()[:10]


def scan_sessions(roots: list[Path], max_depth: int = 2) -> dict[str, SessionRef]:
    """Find run directories (anything holding a session.json) under the roots."""
    found: dict[str, SessionRef] = {}

    def visit(d: Path, depth: int) -> None:
        sj = d / "session.json"
        if sj.is_file():
            try:
                doc = json.loads(sj.read_text())
            except (OSError, json.JSONDecodeError):
                return
            if isinstance(doc, list):  # legacy bare-list format (dict clips only)
                doc = {"clips": [{"index": i, "frame_dir": f"clip_{i:03d}",
                                  "status": "ok", "analysis": c}
                                 for i, c in enumerate(doc) if isinstance(c, dict)],
                       "video": d.name, "facts": {}, "run_stats": {}}
            if not isinstance(doc, dict):
                return  # scalar/null session.json: skip, don't poison every page
            ref = SessionRef(sid=_sid_for(d), dir=d, doc=doc)
            found[ref.sid] = ref
            return  # a run dir doesn't nest more run dirs
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return
        for c in children:
            if c.name in {"node_modules", ".git", ".venv", "__pycache__"}:
                continue
            visit(c, depth + 1)

    for root in roots:
        if root.is_dir():
            visit(root, 0)
    return found


def discover_videos(roots: list[Path]) -> list[Path]:
    vids: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for p in sorted(root.iterdir()):
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                    vids.append(p)
        except OSError:
            continue
    return vids


def safe_child(base: Path, rel: str) -> Path | None:
    """Resolve rel under base, refusing traversal outside it."""
    try:
        target = (base / rel).resolve()
        base_r = base.resolve()
    except OSError:
        return None
    if target == base_r or base_r in target.parents:
        return target
    return None


# ---------------- run manager ----------------

@dataclass
class Run:
    rid: str
    argv: list[str]
    out_dir: Path
    video: str
    started_at: float = field(default_factory=time.time)
    status: str = "running"  # running | done | failed
    returncode: int | None = None
    log: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def append(self, line: str) -> None:
        with self._lock:
            self.log.append(line.rstrip("\n"))
            if len(self.log) > 800:
                del self.log[: len(self.log) - 800]

    def tail(self, n: int = 400) -> list[str]:
        with self._lock:
            return self.log[-n:]


class RunManager:
    """One analysis at a time - a laptop only fits one VLM anyway."""

    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def active(self) -> Run | None:
        with self._lock:
            for r in self.runs.values():
                if r.status == "running":
                    return r
        return None

    def start(self, argv: list[str], out_dir: Path, video: str) -> Run:
        with self._lock:
            for r in self.runs.values():
                if r.status == "running":
                    raise RuntimeError("an analysis is already running")
            rid = hashlib.md5(f"{time.time()}-{video}".encode()).hexdigest()[:8]
            run = Run(rid=rid, argv=argv, out_dir=out_dir, video=video)
            self.runs[rid] = run
        threading.Thread(target=self._work, args=(run,), daemon=True).start()
        return run

    def _work(self, run: Run) -> None:
        run.append("$ " + " ".join(run.argv))
        try:
            # errors="replace": child tools (ffmpeg, yt-dlp, odd filenames) can
            # emit non-UTF-8 bytes; a strict decode would kill this thread and
            # leave the run stuck in "running" forever.
            proc = subprocess.Popen(run.argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    errors="replace", bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                run.append(line)
            run.returncode = proc.wait()
        except Exception as e:  # noqa: BLE001 - any escape would wedge the manager
            run.append(f"runner error: {type(e).__name__}: {e}")
            if run.returncode is None:
                run.returncode = -1
        finally:
            run.status = "done" if run.returncode == 0 else "failed"


# ---------------- HTTP layer ----------------

class AppState:
    def __init__(self, roots: list[Path]):
        self.roots = roots
        self.runs = RunManager()

    def sessions(self) -> dict[str, SessionRef]:
        found = scan_sessions(self.roots)
        # runs may write outside the scan roots (video anywhere on disk):
        # include their out_dirs so completed runs are always reachable
        extra = [r.out_dir for r in self.runs.runs.values()
                 if r.status == "done" and (r.out_dir / "session.json").is_file()]
        if extra:
            found.update(scan_sessions(extra, max_depth=0))
        return found

    def videos(self) -> list[str]:
        return [str(v) for v in discover_videos(self.roots)]


# the only form fields the UI offers; anything else in a POST body is dropped
# (e.g. a smuggled server_url would silently ship frames off-device)
ALLOWED_FORM_FIELDS = {"video", "model", "quick", "offline", "dry_run"}


def _analyze_argv(form: dict[str, str], out_dir: Path) -> list[str]:
    argv = [sys.executable, "-m", "courtside.analyze", "--out", str(out_dir)]
    model = form.get("model") or config.DEFAULT_MODEL_KEY
    argv += ["--model", model]
    if form.get("quick"):
        argv += ["--max-clips", "3"]
    if form.get("offline"):
        argv += ["--offline"]
    if form.get("dry_run"):
        argv += ["--dry-run"]
    if form.get("download_dir"):
        argv += ["--download-dir", form["download_dir"]]
    # "--" ends option parsing so a filename like "-weird.mp4" can't be
    # mistaken for a flag by the child's argparse
    argv += ["--", form["video"]]
    return argv


def build_analysis_request(form: dict[str, str], roots: list[Path]) -> tuple[list[str], Path, str]:
    """Validate the analyze form and produce (argv, out_dir, display label).

    Raises ValueError with a user-facing message on bad input. Accepts either
    a local video path or a YouTube / yt-dlp-supported URL.
    """
    video = (form.get("video") or "").strip()
    if not video:
        raise ValueError("Choose a video, or enter a path or YouTube URL.")

    if is_url(video):
        if form.get("offline"):
            raise ValueError("Offline mode can't download a URL - clear one or the other.")
        if not ytdlp_available():
            raise ValueError("URL input needs yt-dlp: pip install 'courtside[youtube]' and restart.")
        workspace = roots[0] if roots else Path.cwd()
        out_dir = workspace / f"yt_{video_slug(video)}_courtside"
        form = dict(form, video=video, download_dir=str(workspace))
        return _analyze_argv(form, out_dir), out_dir, video

    vp = Path(video).expanduser()
    if not vp.is_file():
        raise ValueError(f"Video not found: {vp}")
    if vp.suffix.lower() not in VIDEO_EXTS:
        raise ValueError(f"Not a video file: {vp.name}")
    out_dir = vp.with_name(vp.stem + "_courtside")
    form = dict(form, video=str(vp))
    return _analyze_argv(form, out_dir), out_dir, vp.name


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "courtside-ui"

        # ---- helpers ----
        def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8",
                  code: int = 200, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            # extra overrides the defaults (never emit a header twice)
            headers = {"Content-Type": ctype, "Content-Length": str(len(body)),
                       "Cache-Control": "no-store", **(extra or {})}
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text: str, code: int = 200) -> None:
            self._send(text.encode(), code=code)

        def _json(self, obj, code: int = 200) -> None:
            self._send(json.dumps(obj).encode(), "application/json", code)

        def _redirect(self, loc: str) -> None:
            self._send(b"", code=303, extra={"Location": loc})

        def _notfound(self) -> None:
            self._html("<h1>404</h1>", 404)

        def log_message(self, fmt, *args):  # quiet access log
            pass

        # ---- routes ----
        def do_GET(self) -> None:
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.split("/") if p]
            try:
                if not parts:
                    return self._dashboard()
                if parts[0] == "session" and len(parts) == 2:
                    return self._session(parts[1])
                if parts[0] == "frames" and len(parts) >= 3:
                    return self._frame(parts[1], "/".join(parts[2:]))
                if parts[0] == "export" and len(parts) == 2:
                    return self._export(parts[1])
                if parts[0] == "run" and len(parts) == 2:
                    return self._run_page(parts[1])
                if parts[0] == "api" and len(parts) == 3 and parts[1] == "runs":
                    return self._run_status(parts[2])
                if parts[0] == "api" and parts[1:] == ["sessions"]:
                    return self._json([{"sid": s.sid, "title": s.title} for s in state.sessions().values()])
                return self._notfound()
            except BrokenPipeError:
                pass

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if url.path != "/analyze":
                return self._notfound()
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._html(render_dashboard(state, error="Malformed request."), 400)
            raw = self.rfile.read(min(max(length, 0), 1 << 20)).decode(errors="replace")
            form = {k: v[0] for k, v in parse_qs(raw).items() if k in ALLOWED_FORM_FIELDS}
            try:
                argv, out_dir, label = build_analysis_request(form, state.roots)
            except ValueError as e:
                return self._html(render_dashboard(state, error=str(e)), 400)
            try:
                run = state.runs.start(argv, out_dir, label)
            except RuntimeError as e:
                return self._html(render_dashboard(state, error=str(e)), 409)
            return self._redirect(f"/run/{run.rid}")

        # ---- views ----
        def _dashboard(self) -> None:
            self._html(render_dashboard(state))

        def _session(self, sid: str) -> None:
            ref = state.sessions().get(sid)
            if not ref:
                return self._notfound()
            self._html(render_session(ref))

        def _frame(self, sid: str, rel: str) -> None:
            ref = state.sessions().get(sid)
            if not ref:
                return self._notfound()
            target = safe_child(ref.dir, rel)
            if target is None or not target.is_file() or target.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                return self._notfound()
            ctype = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
            self._send(target.read_bytes(), ctype, extra={"Cache-Control": "max-age=3600"})

        def _export(self, sid: str) -> None:
            """Regenerate + serve the self-contained report.html for sharing."""
            ref = state.sessions().get(sid)
            if not ref:
                return self._notfound()
            from .report_html import render_html
            self._send(render_html(ref.dir, ref.doc).encode())

        def _run_page(self, rid: str) -> None:
            run = state.runs.runs.get(rid)
            if not run:
                return self._notfound()
            self._html(render_run_page(run))

        def _run_status(self, rid: str) -> None:
            run = state.runs.runs.get(rid)
            if not run:
                return self._json({"error": "unknown run"}, 404)
            # only link a session when THIS run produced one: a dry run leaves
            # no session.json, and a stale file from an earlier run shouldn't
            # count either - require it newer than the run start
            sid = None
            if run.status == "done":
                sj = run.out_dir / "session.json"
                try:
                    if sj.is_file() and sj.stat().st_mtime >= run.started_at - 1:
                        sid = _sid_for(run.out_dir)
                except OSError:
                    pass
            self._json({
                "rid": run.rid, "status": run.status, "returncode": run.returncode,
                "elapsed_s": round(time.time() - run.started_at, 1),
                "log": run.tail(), "session": sid, "video": run.video,
            })

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="courtside-ui", description=__doc__)
    p.add_argument("--root", type=Path, action="append", default=None,
                   help="workspace dir(s) to scan for videos and runs (default: cwd)")
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)

    roots = args.root or [Path.cwd()]
    # always include the bundled sample if we can find it (repo checkout)
    for cand in [Path.cwd() / "examples", Path(__file__).resolve().parent.parent / "examples"]:
        if cand.is_dir() and cand not in roots:
            roots.append(cand)

    state = AppState(roots)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    url = f"http://127.0.0.1:{args.port}"
    print(f"courtside-ui serving {url}  (roots: {', '.join(str(r) for r in roots)})")
    print("local only - nothing leaves this machine. Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
