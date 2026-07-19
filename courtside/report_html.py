"""Self-contained HTML report - the visual layer for the demo.

Renders a single file (inline CSS, base64-embedded JPEGs, stdlib only) from a
session doc plus the frames already on disk:
  - header with on-device badge + the cost/throughput line
  - an SVG timeline (rally blocks + stroke ticks colored by max flag severity)
  - per-clip cards with thumbnails and rally summary
  - a "Flagged Moments" gallery: the nearest keyframe to each medium/high flag,
    with a severity chip and the model's evidence quote
  - the LLM Markdown report, embedded

Everything here is derived from structured data + frame files, so it can be
(re)built with zero model calls (finding: no visual layer; no instant demo mode).
"""

from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path
from typing import Any

_SEV_COLOR = {"high": "#e5484d", "medium": "#f5a623", "low": "#8a8f98"}


def _img_data_uri(path: Path) -> str | None:
    try:
        b64 = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except OSError:
        return None


def _clip_frames(out_dir: Path, clip: dict[str, Any]) -> list[Path]:
    frame_dir = out_dir / clip.get("frame_dir", f"clip_{clip.get('index', 0):03d}")
    return sorted(frame_dir.glob("frame_*.jpg"))


def _frame_for_time(frames: list[Path], start_s: float, fps_used: float, t_s: float) -> Path | None:
    if not frames:
        return None
    if not fps_used or fps_used <= 0:
        return frames[len(frames) // 2]
    idx = round((t_s - start_s) * fps_used)
    idx = max(0, min(len(frames) - 1, idx))
    return frames[idx]


def _md_to_html(md: str) -> str:
    """Tiny Markdown subset -> HTML (headers, bold, hr, bullet lists, paragraphs).

    Consecutive plain lines join into one paragraph, and a wrapped line inside a
    list continues its <li> - source-wrapped prose must not fragment.
    """
    out: list[str] = []
    list_tag: str | None = None  # "ul" | "ol" | None
    para: list[str] = []
    _ordered = re.compile(r"^\d+\.\s+")

    def close_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def open_list(tag: str, start: int = 1) -> None:
        nonlocal list_tag
        if list_tag != tag:
            close_list()
            # honor the source numbering so "1./2./3." split by blank lines
            # doesn't render as 1./1./1.
            out.append(f'<ol start="{start}">' if tag == "ol" and start != 1 else f"<{tag}>")
            list_tag = tag

    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            close_para()
            close_list()
            continue
        m_ord = _ordered.match(stripped)
        if line.startswith(("# ", "## ", "### ")) or stripped == "---":
            close_para()
            close_list()
            if line.startswith("### "):
                out.append(f"<h3>{html.escape(line[4:])}</h3>")
            elif line.startswith("## "):
                out.append(f"<h2>{html.escape(line[3:])}</h2>")
            elif line.startswith("# "):
                out.append(f"<h1>{html.escape(line[2:])}</h1>")
            else:
                out.append("<hr>")
        elif line.lstrip().startswith(("- ", "* ")):
            close_para()
            open_list("ul")
            out.append(f"<li>{_inline(line.lstrip()[2:])}</li>")
        elif m_ord:
            close_para()
            open_list("ol", start=int(m_ord.group().rstrip(". ") or 1))
            out.append(f"<li>{_inline(stripped[m_ord.end():])}</li>")
        elif list_tag:
            # continuation of a wrapped list item
            out[-1] = out[-1][:-5] + " " + _inline(stripped) + "</li>"
        else:
            para.append(stripped)
    close_para()
    close_list()
    return "\n".join(out)


def _inline(text: str) -> str:
    esc = html.escape(text)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
    esc = re.sub(r"`(.+?)`", r"<code>\1</code>", esc)
    return esc


def _severity_rank(flag: dict[str, Any]) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(flag.get("severity", "low"), 1)


def _timeline_svg(session: dict[str, Any], activity: dict[str, Any] | None) -> str:
    clips = session.get("clips", [])
    duration = session.get("video_duration_s") or 1.0
    W, H = 960, 90
    pad = 8
    inner_w = W - 2 * pad

    def x(t: float) -> float:
        return pad + inner_w * max(0.0, min(1.0, t / duration))

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="none" role="img" aria-label="session timeline">']
    parts.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="var(--panel)" rx="8"/>')

    # activity sparkline (motion energy) as background
    if activity and activity.get("times") and activity.get("scores"):
        ts, sc = activity["times"], activity["scores"]
        mx = max(sc) or 1.0
        base = H - 22
        pts = " ".join(f"{x(t):.1f},{base - (s / mx) * (base - pad):.1f}" for t, s in zip(ts, sc))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--muted)" stroke-width="1" opacity="0.55"/>')

    # rally blocks
    for c in clips:
        if not isinstance(c, dict):
            continue
        a = c.get("analysis")
        if not isinstance(a, dict):
            continue
        s0, s1 = a.get("start_s", 0.0), a.get("end_s", 0.0)
        parts.append(
            f'<rect x="{x(s0):.1f}" y="{H-20}" width="{max(2, x(s1)-x(s0)):.1f}" height="12" '
            f'fill="var(--accent)" opacity="0.35" rx="2"/>'
        )
        # stroke ticks colored by worst flag severity
        for st in a.get("strokes", []):
            flags = (st.get("technique_flags", []) or []) + (st.get("tactical_flags", []) or [])
            worst = max((f.get("severity", "low") for f in flags), key=lambda s: {"low": 1, "medium": 2, "high": 3}.get(s, 1), default="low") if flags else None
            color = _SEV_COLOR.get(worst, "var(--fg)") if worst else "var(--fg)"
            tx = x(st.get("t_s", s0))
            parts.append(f'<line x1="{tx:.1f}" y1="{pad}" x2="{tx:.1f}" y2="{H-22}" stroke="{color}" stroke-width="1.5" opacity="0.85"/>')
    parts.append("</svg>")
    return "".join(parts)


def collect_flag_moments(
    out_dir: Path,
    session: dict[str, Any],
    min_sev: str = "medium",
    cap: int = 12,
    img_fn=None,
) -> list[dict[str, Any]]:
    """Flagged strokes paired with their nearest keyframe.

    ``img_fn(frame_path) -> str | None`` controls how the image is referenced;
    the default embeds a base64 data URI (self-contained export). The web UI
    passes a URL builder instead.
    """
    if img_fn is None:
        img_fn = _img_data_uri
    order = {"low": 1, "medium": 2, "high": 3}
    threshold = order[min_sev]
    moments: list[dict[str, Any]] = []
    for c in session.get("clips", []):
        if not isinstance(c, dict):
            continue
        a = c.get("analysis")
        if not isinstance(a, dict):
            continue
        frames = _clip_frames(out_dir, c)
        for st in a.get("strokes", []):
            for kind in ("technique_flags", "tactical_flags"):
                for f in st.get(kind, []) or []:
                    if order.get(f.get("severity", "low"), 1) >= threshold:
                        fp = _frame_for_time(frames, a.get("start_s", 0.0), c.get("fps_used", 0.0), st.get("t_s", 0.0))
                        moments.append({
                            "code": f.get("code", ""),
                            "severity": f.get("severity", "low"),
                            "evidence": f.get("evidence", ""),
                            "t_s": st.get("t_s", 0.0),
                            "stroke": st.get("stroke", "unknown"),
                            "player": st.get("player", "unknown"),
                            "kind": "technique" if kind == "technique_flags" else "tactical",
                            "img": img_fn(fp) if fp else None,
                        })
    moments.sort(key=lambda m: order.get(m["severity"], 1), reverse=True)
    return moments[:cap]


_collect_flag_moments = collect_flag_moments  # back-compat alias


_CSS = """
:root{--bg:#0f1115;--fg:#e6e8eb;--muted:#8a8f98;--panel:#181b21;--accent:#3b82f6;--border:#262a31;}
@media (prefers-color-scheme: light){:root{--bg:#f7f8fa;--fg:#1b1d21;--muted:#6b7280;--panel:#ffffff;--accent:#2563eb;--border:#e5e7eb;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:32px 20px 80px}
h1,h2,h3{line-height:1.25}
.hd{display:flex;flex-wrap:wrap;align-items:baseline;gap:12px;justify-content:space-between}
.title{font-size:26px;font-weight:700;margin:0}
.sub{color:var(--muted);font-size:14px}
.badge{display:inline-block;background:var(--panel);border:1px solid var(--border);border-radius:999px;padding:4px 12px;font-size:12px;color:var(--muted)}
.badge.on{color:#16a34a;border-color:#16a34a55}
.cost{margin:18px 0 6px;padding:14px 16px;background:var(--panel);border:1px solid var(--border);border-radius:10px;font-size:15px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;margin:16px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.thumb{width:100%;border-radius:6px;display:block;border:1px solid var(--border)}
.chip{display:inline-block;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:600;color:#fff}
.mcard{background:var(--panel);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.mcard img{width:100%;display:block;aspect-ratio:16/9;object-fit:cover;background:#000}
.mbody{padding:10px 12px}
.mcode{font-weight:600;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.mev{color:var(--muted);font-size:13px;margin-top:4px}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.stat{display:inline-block;margin-right:22px}
.stat b{font-size:22px;display:block}
.stat span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.rep h1{font-size:22px;border-bottom:1px solid var(--border);padding-bottom:8px}
.rep h2{font-size:18px;margin-top:24px}
code{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:13px}
.muted{color:var(--muted)}
"""


def render_html(out_dir: Path, session: dict[str, Any]) -> str:
    activity = None
    apath = out_dir / "activity.json"
    if apath.exists():
        try:
            activity = json.loads(apath.read_text())
        except (OSError, json.JSONDecodeError):
            activity = None

    rs = session.get("run_stats", {})
    facts = session.get("facts", {})
    split = facts.get("player_split", {})
    on_device = session.get("on_device", False)

    from .report import cost_summary_line
    cost_line = cost_summary_line(rs) if rs.get("total_tokens") is not None else ""

    parts: list[str] = []
    parts.append(f"<style>{_CSS}</style>")
    parts.append('<div class="wrap">')

    # header
    badge = '<span class="badge on">on-device - network not used</span>' if on_device else '<span class="badge">server backend</span>'
    parts.append('<div class="hd">')
    parts.append(f'<h1 class="title">Courtside - Session Analysis</h1>{badge}</div>')
    parts.append(
        f'<div class="sub">{html.escape(str(session.get("video", "")))} - '
        f'model {html.escape(str(session.get("model", "")))} - '
        f'{html.escape(str(session.get("created_at") or ""))}</div>'
    )
    if cost_line:
        parts.append(f'<div class="cost">{html.escape(cost_line)}</div>')

    # stat row
    parts.append('<div class="panel">')
    parts.append(f'<span class="stat"><b>{facts.get("total_strokes", 0)}</b><span>strokes</span></span>')
    parts.append(f'<span class="stat"><b>{facts.get("clips_analyzed", 0)}</b><span>rallies</span></span>')
    parts.append(f'<span class="stat"><b>{split.get("near", 0)}/{split.get("far", 0)}</b><span>near / far</span></span>')
    if rs.get("realtime_factor"):
        parts.append(f'<span class="stat"><b>{rs["realtime_factor"]}x</b><span>realtime</span></span>')
    if rs.get("peak_gb"):
        parts.append(f'<span class="stat"><b>{rs["peak_gb"]} GB</b><span>peak memory</span></span>')
    if rs.get("clips_failed"):
        parts.append(f'<span class="stat"><b>{rs["clips_failed"]}</b><span>clips skipped</span></span>')
    parts.append("</div>")

    # timeline
    parts.append('<div class="panel"><h2 style="margin-top:0">Session timeline</h2>')
    parts.append('<div class="muted" style="font-size:13px;margin-bottom:8px">Rally blocks below; stroke ticks above, colored by worst flag (red high / amber medium).</div>')
    parts.append(_timeline_svg(session, activity))
    parts.append("</div>")

    # flagged moments
    moments = _collect_flag_moments(out_dir, session)
    if moments:
        parts.append('<h2>Flagged moments</h2><div class="mgrid">')
        for m in moments:
            color = _SEV_COLOR.get(m["severity"], "#8a8f98")
            img = f'<img src="{m["img"]}" alt="frame">' if m["img"] else '<div style="aspect-ratio:16/9;background:#000"></div>'
            parts.append(
                f'<div class="mcard">{img}<div class="mbody">'
                f'<span class="chip" style="background:{color}">{html.escape(m["severity"])}</span> '
                f'<span class="mcode">{html.escape(m["code"])}</span>'
                f'<div class="mev">{html.escape(m["stroke"])} ({html.escape(m["player"])}) @ t={m["t_s"]:.1f}s</div>'
                f'<div class="mev">{html.escape(m["evidence"])}</div>'
                f'</div></div>'
            )
        parts.append("</div>")

    # per-clip cards
    parts.append("<h2>Rallies</h2>")
    for c in session.get("clips", []):
        a = c.get("analysis", {})
        status = c.get("status", "ok")
        if status != "ok":
            parts.append(f'<div class="panel"><b>Clip {c.get("index", "?")}</b> <span class="muted">skipped ({html.escape(str(c.get("error", "")))})</span></div>')
            continue
        frames = _clip_frames(out_dir, c)
        thumbs = frames[:: max(1, len(frames) // 4)][:4] if frames else []
        parts.append('<div class="panel">')
        parts.append(
            f'<b>Clip {c.get("index", "?")}</b> <span class="muted">{a.get("start_s", 0):.1f}-{a.get("end_s", 0):.1f}s - '
            f'{len(a.get("strokes", []))} strokes - confidence {html.escape(str(a.get("confidence", "")))}</span>'
        )
        if a.get("rally_summary"):
            parts.append(f'<p style="margin:8px 0">{html.escape(a["rally_summary"])}</p>')
        if thumbs:
            parts.append('<div class="grid">')
            for t in thumbs:
                uri = _img_data_uri(t)
                if uri:
                    parts.append(f'<img class="thumb" src="{uri}" alt="frame">')
            parts.append("</div>")
        parts.append("</div>")

    # embedded markdown report
    md_path = out_dir / "session_report.md"
    if md_path.exists():
        parts.append('<div class="rep">')
        parts.append(_md_to_html(md_path.read_text()))
        parts.append("</div>")

    parts.append("</div>")
    return "<!-- courtside report -->\n" + "".join(parts)


def write_html_report(out_dir: Path, session: dict[str, Any]) -> Path:
    path = out_dir / "report.html"
    path.write_text(render_html(out_dir, session))
    return path
