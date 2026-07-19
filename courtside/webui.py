"""Views for courtside-ui: the SaaS-style dashboard, session, and run pages.

Pure functions dict-in/HTML-out so they render (and test) without a server.
Design tokens follow the repo's data-viz conventions: status colors carry
severity (always paired with a text label, never color alone), one accent hue,
recessive chrome, light/dark from the same steps with a viewer toggle that
wins over the OS setting in both directions.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from . import config
from .report import cost_summary_line
from .report_html import _clip_frames, _md_to_html, collect_flag_moments

# severity -> status color (light, dark). Labels always accompany the color.
SEV = {
    "high":   ("#d03b3b", "#d03b3b", "critical"),
    "medium": ("#fab219", "#fab219", "warning"),
    "low":    ("#898781", "#898781", "minor"),
}
ACCENT_L, ACCENT_D = "#2a78d6", "#3987e5"


def _e(x: Any) -> str:
    return html.escape(str(x), quote=True)


# ---------------- layout ----------------

_CSS = """
:root{
  color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --border:rgba(11,11,11,.10); --accent:#2a78d6; --accent-ink:#fff;
  --good-text:#006300; --chip-critical:#d03b3b; --chip-warning:#fab219; --chip-minor:#898781;
  --console:#141414; --console-ink:#d6d6d0;
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --border:rgba(255,255,255,.10); --accent:#3987e5;
  --good-text:#0ca30c;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --border:rgba(255,255,255,.10); --accent:#3987e5;
    --good-text:#0ca30c;
  }
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--page);color:var(--ink);
  font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

.app{display:flex;min-height:100vh}
.side{width:212px;flex:0 0 212px;border-right:1px solid var(--border);
  padding:18px 14px;display:flex;flex-direction:column;gap:4px;background:var(--surface)}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:16px;
  letter-spacing:-.01em;margin:2px 6px 16px}
.brand .ball{width:14px;height:14px;border-radius:50%;background:#b7d332;
  box-shadow:inset -2px -2px 0 rgba(0,0,0,.14);position:relative;flex:0 0 14px}
.nav a{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;
  color:var(--ink-2);font-weight:500}
.nav a:hover{background:color-mix(in srgb,var(--ink) 5%,transparent);text-decoration:none}
.nav a.on{background:color-mix(in srgb,var(--accent) 12%,transparent);color:var(--ink)}
.nav svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:1.7;flex:0 0 16px}
.side .foot{margin-top:auto;font-size:12px;color:var(--muted);padding:8px 0}
.side .foot .pill{white-space:nowrap;font-size:11px;padding:3px 8px}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--border);
  border-radius:999px;padding:3px 10px;font-size:12px;color:var(--ink-2);background:var(--surface)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--good-text)}

.main{flex:1;min-width:0;padding:22px 28px 60px;max-width:1160px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
.topbar h1{font-size:19px;margin:0;letter-spacing:-.01em}
.crumb{color:var(--muted)}
.tbtns{display:flex;gap:8px;align-items:center}
button,.btn{border:1px solid var(--border);background:var(--surface);color:var(--ink);
  border-radius:8px;padding:7px 13px;font:inherit;font-weight:500;cursor:pointer}
button:hover,.btn:hover{background:color-mix(in srgb,var(--ink) 4%,var(--surface));text-decoration:none}
.btn-pri{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn-pri:hover{background:color-mix(in srgb,black 12%,var(--accent))}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0 22px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tile b{display:block;font-size:26px;font-weight:650;letter-spacing:-.02em;line-height:1.2}
.tile span{color:var(--ink-2);font-size:12px}
.tile .sub{color:var(--muted);font-size:11px;margin-top:2px}

.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:18px;margin-bottom:18px}
.card h2{margin:0 0 4px;font-size:15px}
.card .hint{color:var(--muted);font-size:12.5px;margin:0 0 12px}

.banner{border:1px solid var(--border);border-left:3px solid var(--accent);
  background:var(--surface);border-radius:10px;padding:11px 14px;margin-bottom:16px;font-size:13.5px}
.banner.err{border-left-color:var(--chip-critical)}

.sess{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.scard{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:14px 16px;display:block;color:var(--ink)}
.scard:hover{text-decoration:none;border-color:color-mix(in srgb,var(--accent) 45%,var(--border))}
.scard .t{font-weight:600;margin-bottom:2px}
.scard .m{color:var(--muted);font-size:12px}
.scard .row{display:flex;gap:14px;margin-top:10px;font-size:12.5px;color:var(--ink-2)}
.scard .row b{font-weight:650;color:var(--ink)}

form.analyze{display:grid;grid-template-columns:1fr 1fr;gap:12px}
form.analyze .full{grid-column:1/-1}
label.f{display:block;font-size:12px;color:var(--ink-2);margin-bottom:4px;font-weight:550}
input[type=text],select{width:100%;border:1px solid var(--border);background:var(--page);
  color:var(--ink);border-radius:8px;padding:8px 10px;font:inherit}
.checks{display:flex;gap:18px;flex-wrap:wrap;align-items:center;color:var(--ink-2);font-size:13px}
.checks label{display:inline-flex;gap:6px;align-items:center;cursor:pointer}

.chip{display:inline-flex;align-items:center;gap:5px;border-radius:6px;padding:1px 8px;
  font-size:11.5px;font-weight:600;color:#fff}
.chip svg{width:10px;height:10px;fill:currentColor}
.chip.minor{color:var(--ink)}
.kicker{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  font-weight:650;margin:26px 0 10px}

.timeline-wrap{overflow-x:auto}
.legend{display:flex;gap:16px;align-items:center;font-size:12px;color:var(--ink-2);margin-top:8px}
.legend .sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}

.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.mcard{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.mcard img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#000}
.mcard .b{padding:10px 12px}
.mcard .code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;font-weight:600}
.mcard .ev{color:var(--ink-2);font-size:12.5px;margin-top:5px}
.mcard .mm{color:var(--muted);font-size:11.5px;margin-top:3px}

.rally{display:flex;gap:14px;align-items:flex-start}
.rally .fr{display:flex;gap:6px;flex:0 0 auto}
.rally .fr img{width:104px;border-radius:6px;border:1px solid var(--border);display:block}
.rally .t{font-weight:600}
.rally .m{color:var(--muted);font-size:12px}
.rally p{margin:6px 0 0;color:var(--ink-2);font-size:13px}

.rep{max-width:760px}
.rep h1{font-size:20px;border-bottom:1px solid var(--grid);padding-bottom:8px}
.rep h2{font-size:15.5px;margin-top:22px}
.rep code{background:var(--page);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:12.5px}
.rep hr{border:0;border-top:1px solid var(--grid)}

.console{background:var(--console);color:var(--console-ink);border-radius:12px;
  padding:14px 16px;font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;
  min-height:200px;max-height:460px;overflow-y:auto;white-space:pre-wrap;word-break:break-word}
.status-row{display:flex;gap:10px;align-items:center;margin-bottom:12px}
.spin{width:14px;height:14px;border:2px solid var(--grid);border-top-color:var(--accent);
  border-radius:50%;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

#tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--border);
  border-radius:8px;padding:7px 10px;font-size:12px;color:var(--ink);box-shadow:0 4px 14px rgba(0,0,0,.18);
  opacity:0;transition:opacity .08s;z-index:50;max-width:260px}
#tip .h{font-weight:650}
#tip .s{color:var(--muted)}
@media (max-width:760px){.app{flex-direction:column}.side{width:auto;flex-direction:row;align-items:center}
  .side .foot,.nav svg{display:none}.nav{display:flex;gap:4px}}
"""

_JS = """
(function(){
  var t = localStorage.getItem('cs-theme');
  if (t) document.documentElement.dataset.theme = t;
})();
function toggleTheme(){
  var cur = document.documentElement.dataset.theme ||
    (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  var next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('cs-theme', next);
}
document.addEventListener('DOMContentLoaded', function(){
  var tip = document.createElement('div'); tip.id = 'tip'; document.body.appendChild(tip);
  document.querySelectorAll('[data-tip]').forEach(function(el){
    el.addEventListener('mousemove', function(ev){
      tip.innerHTML = el.dataset.tip;
      tip.style.opacity = 1;
      var x = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8);
      var y = Math.min(ev.clientY + 14, innerHeight - tip.offsetHeight - 8);
      tip.style.left = x + 'px'; tip.style.top = y + 'px';
    });
    el.addEventListener('mouseleave', function(){ tip.style.opacity = 0; });
  });
});
"""

_ICONS = {
    "dash": '<svg viewBox="0 0 24 24"><path d="M3 12h6V3H3zM15 21h6v-9h-6zM3 21h6v-5H3zM15 8h6V3h-6z"/></svg>',
    "new": '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
    "doc": '<svg viewBox="0 0 24 24"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8zM14 3v5h5M9 13h6M9 17h6"/></svg>',
}


def _page(title: str, body: str, active: str = "", crumb: str = "") -> str:
    # crumb carries user data on the run page (filename / pasted URL) - escape it
    crumb_html = f'<span class="crumb">{_e(crumb)}</span>' if crumb else ""
    favicon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
               "%3Ccircle cx='8' cy='8' r='7' fill='%23b7d332'/%3E"
               "%3Cpath d='M2 4 Q8 8 2 12 M14 4 Q8 8 14 12' stroke='white' stroke-width='1.2' fill='none'/%3E%3C/svg%3E")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} - Courtside</title>
<link rel="icon" href="{favicon}">
<style>{_CSS}</style><script>{_JS}</script></head>
<body><div class="app">
<aside class="side">
  <div class="brand"><span class="ball"></span> Courtside</div>
  <nav class="nav">
    <a href="/" class="{'on' if active == 'dash' else ''}">{_ICONS['dash']} Dashboard</a>
    <a href="/#new" class="{'on' if active == 'new' else ''}">{_ICONS['new']} New analysis</a>
    <a href="/#sessions">{_ICONS['doc']} Sessions</a>
  </nav>
  <div class="foot"><span class="pill"><span class="dot"></span> on-device &middot; 0 bytes uploaded</span></div>
</aside>
<main class="main">
<div class="topbar"><h1>{_e(title)} {crumb_html}</h1>
  <div class="tbtns"><button onclick="toggleTheme()" title="toggle theme">&#9681;</button></div>
</div>
{body}
</main></div></body></html>"""


def _sev_chip(severity: str) -> str:
    color_l, _, label = SEV.get(severity, SEV["low"])
    cls = "chip minor" if severity == "low" else "chip"
    icon = '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'
    return f'<span class="{cls}" style="background:{color_l}{";opacity:.9" if severity=="low" else ""}">{icon}{label}</span>'


# ---------------- dashboard ----------------

def _fmt_min(seconds: float | None) -> str:
    if not seconds:
        return "0"
    return f"{seconds/60:.0f}"


def _num(x) -> float:
    """Doc values come from disk and may be mistyped - count only real numbers."""
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def _dget(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else default


def render_dashboard(state, error: str | None = None) -> str:
    sessions = sorted(state.sessions().values(),
                      key=lambda r: str(_dget(r.doc, "created_at") or ""), reverse=True)
    videos = state.videos()

    n_strokes = int(sum(_num(_dget(_dget(r.doc, "facts") or {}, "total_strokes")) for r in sessions))
    n_flags = 0
    for r in sessions:
        f = _dget(r.doc, "facts") or {}
        for key in ("top_technique_flags", "top_tactical_flags"):
            counts = _dget(f, key) or {}
            if isinstance(counts, dict):
                n_flags += int(sum(_num(v) for v in counts.values()))
    minutes = sum(_num(_dget(r.doc, "video_duration_s")) for r in sessions)
    rts = [_num(_dget(_dget(r.doc, "run_stats") or {}, "realtime_factor")) for r in sessions]
    rts = [x for x in rts if x > 0]
    avg_rt = f"{sum(rts)/len(rts):.1f}&times;" if rts else "&mdash;"

    parts: list[str] = []
    if error:
        parts.append(f'<div class="banner err">{_e(error)}</div>')
    active = state.runs.active()
    if active:
        parts.append(f'<div class="banner">Analysis of <b>{_e(active.video)}</b> is running &mdash; '
                     f'<a href="/run/{active.rid}">watch progress</a></div>')

    parts.append(f"""
<div class="tiles">
  <div class="tile"><b>{len(sessions)}</b><span>sessions</span></div>
  <div class="tile"><b>{_fmt_min(minutes)}</b><span>minutes analyzed</span></div>
  <div class="tile"><b>{n_strokes}</b><span>strokes</span></div>
  <div class="tile"><b>{n_flags}</b><span>coaching flags</span></div>
  <div class="tile"><b>{avg_rt}</b><span>avg speed vs realtime</span><div class="sub">on this machine</div></div>
</div>""")

    # new analysis card
    vid_opts = "".join(f'<option value="{_e(v)}">{_e(Path(v).name)}</option>' for v in videos)
    model_opts = "".join(
        f'<option value="{_e(k)}"{" selected" if k == config.DEFAULT_MODEL_KEY else ""}>'
        f'{_e(k)} &mdash; ~{spec.approx_weights_gb:.0f} GB</option>'
        for k, spec in config.MODELS.items())
    parts.append(f"""
<div class="card" id="new">
  <h2>New analysis</h2>
  <p class="hint">Analysis runs entirely on this machine. Pick a workspace video, paste a path,
    or paste a YouTube link &mdash; the download fetches over the network, the footage never leaves this laptop.</p>
  <form class="analyze" method="post" action="/analyze">
    <div><label class="f" for="vsel">Video in workspace</label>
      <select id="vsel" onchange="document.getElementById('vpath').value=this.value">
        <option value="">&mdash; choose &mdash;</option>{vid_opts}</select></div>
    <div><label class="f" for="vpath">&hellip;or path / YouTube URL</label>
      <input type="text" id="vpath" name="video"
             placeholder="/path/to/match.mp4  or  https://youtube.com/watch?v=..."></div>
    <div><label class="f" for="model">Model</label>
      <select id="model" name="model">{model_opts}</select></div>
    <div class="checks" style="align-self:end;padding-bottom:6px">
      <label><input type="checkbox" name="quick" value="1" checked> quick pass (3 clips)</label>
      <label><input type="checkbox" name="offline" value="1"> offline</label>
      <label><input type="checkbox" name="dry_run" value="1"> plumbing only</label>
    </div>
    <div class="full"><button class="btn-pri" type="submit">Analyze</button></div>
  </form>
</div>""")

    # sessions grid
    parts.append('<div class="kicker" id="sessions">Sessions</div>')
    if not sessions:
        parts.append('<div class="card"><p class="hint" style="margin:0">No sessions yet - run your first analysis above.</p></div>')
    else:
        cards = []
        for r in sessions:
            facts = _dget(r.doc, "facts") or {}
            rs = _dget(r.doc, "run_stats") or {}
            model = str(_dget(r.doc, "model") or "").split("/")[-1]
            when = str(_dget(r.doc, "created_at") or "")[:10]
            clips = _dget(r.doc, "clips") or []
            n_clip = _dget(facts, "clips_analyzed", len(clips) if isinstance(clips, list) else 0)
            rt = _dget(rs, "realtime_factor")
            cards.append(f"""
<a class="scard" href="/session/{_e(r.sid)}">
  <div class="t">{_e(r.title)}</div>
  <div class="m">{_e(when)} &middot; {_e(model)}</div>
  <div class="row"><span><b>{_e(_dget(facts, "total_strokes", 0))}</b> strokes</span>
    <span><b>{_e(n_clip)}</b> rallies</span>
    <span><b>{_e(rt) if rt else "&mdash;"}</b>&times; realtime</span></div>
</a>""")
        parts.append(f'<div class="sess">{"".join(cards)}</div>')

    return _page("Dashboard", "".join(parts), active="dash")


# ---------------- session page ----------------

def _timeline(sid: str, doc: dict, activity: dict | None) -> str:
    clips = [c for c in _dget(doc, "clips") or []
             if isinstance(c, dict) and isinstance(c.get("analysis"), dict)]
    duration = _num(_dget(doc, "video_duration_s")) or max(
        (_num(c["analysis"].get("end_s")) for c in clips), default=0) or 1.0
    W, H, pad, axis_h = 1080, 132, 14, 22
    inner = W - 2 * pad
    plot_h = H - axis_h

    def x(t: float) -> float:
        return pad + inner * max(0.0, min(1.0, t / duration))

    s: list[str] = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="min-width:680px" role="img" aria-label="session timeline">']
    # gridlines + axis labels: pick a step that yields <= ~10 labels
    step = next((c for c in (15, 30, 60, 120, 300, 600, 900, 1800, 3600)
                 if duration / c <= 10), 3600)
    tks = int(duration // step)
    for i in range(tks + 1):
        t = i * step
        s.append(f'<line x1="{x(t):.1f}" y1="{pad}" x2="{x(t):.1f}" y2="{plot_h}" stroke="var(--grid)" stroke-width="1"/>')
        lbl = f"{int(t//60)}:{int(t%60):02d}"
        s.append(f'<text x="{x(t):.1f}" y="{H-6}" font-size="11" fill="var(--muted)" text-anchor="middle" style="font-variant-numeric:tabular-nums">{lbl}</text>')
    # activity sparkline (guard both keys: the file is user-modifiable on disk)
    if isinstance(activity, dict) and activity.get("times") and activity.get("scores"):
        ts, sc = activity["times"], activity["scores"]
        mx = max(sc, default=0) or 1.0
        pts = " ".join(f"{x(t):.1f},{plot_h - 8 - (v/mx)*(plot_h-pad-24):.1f}" for t, v in zip(ts, sc))
        s.append(f'<polyline points="{pts}" fill="none" stroke="var(--muted)" stroke-width="1" opacity="0.4"/>')
    # rally blocks + stroke ticks with hover targets
    for c in clips:
        a = c["analysis"]
        s0, s1 = a.get("start_s", 0.0), a.get("end_s", 0.0)
        s.append(f'<a href="#clip-{_e(c.get("index", 0))}"><rect x="{x(s0):.1f}" y="{plot_h-16}" '
                 f'width="{max(3, x(s1)-x(s0)):.1f}" height="10" rx="2" fill="var(--accent)" opacity="0.35">'
                 f'</rect></a>')
        strokes = a.get("strokes", [])
        xs = [x(st.get("t_s", s0)) for st in strokes]
        for j, st in enumerate(strokes):
            flags = (st.get("technique_flags") or []) + (st.get("tactical_flags") or [])
            worst = "none"
            for f in flags:
                sev = f.get("severity", "low")
                if worst == "none" or {"low": 1, "medium": 2, "high": 3}.get(sev, 0) > {"low": 1, "medium": 2, "high": 3}.get(worst, 0):
                    worst = sev
            color = SEV.get(worst, SEV["low"])[0] if worst != "none" else "var(--ink-2)"
            tx = xs[j]
            # hover hit target: wide for easy hovering, but never overlapping the
            # neighbor stroke's target (overlap makes close strokes unhoverable)
            gap = min((abs(tx - xs[k]) for k in range(len(xs)) if k != j), default=24.0)
            half = max(2.0, min(6.0, gap / 2 - 0.5))
            tip = f"<span class='h'>{_e(st.get('stroke', '?'))}</span> ({_e(st.get('player', '?'))}) &middot; t={st.get('t_s', 0):.1f}s"
            if flags:
                tip += "<br>" + "<br>".join(f"{_e(f.get('code', ''))} <span class='s'>({_e(f.get('severity', ''))})</span>" for f in flags[:4])
            s.append(f'<line x1="{tx:.1f}" y1="{pad+4}" x2="{tx:.1f}" y2="{plot_h-20}" stroke="{color}" stroke-width="2"/>')
            s.append(f'<rect x="{tx-half:.1f}" y="{pad}" width="{2*half:.1f}" height="{plot_h-pad-16}" fill="transparent" data-tip="{_e(tip)}"/>')
    s.append("</svg>")
    legend = (
        '<div class="legend">'
        f'<span><span class="sw" style="background:{ACCENT_L};opacity:.5"></span>rally</span>'
        f'<span><span class="sw" style="background:{SEV["high"][0]}"></span>critical flag</span>'
        f'<span><span class="sw" style="background:{SEV["medium"][0]}"></span>warning flag</span>'
        f'<span><span class="sw" style="background:{SEV["low"][0]}"></span>minor / clean</span>'
        "<span style='color:var(--muted)'>hover a stroke for details; click a rally to jump</span></div>"
    )
    return f'<div class="card"><h2>Session timeline</h2><div class="timeline-wrap">{"".join(s)}</div>{legend}</div>'


def render_session(ref) -> str:
    doc, sdir, sid = ref.doc, ref.dir, ref.sid
    facts = _dget(doc, "facts") or {}
    rs = _dget(doc, "run_stats") or {}
    split = _dget(facts, "player_split") or {}

    activity = None
    ap = sdir / "activity.json"
    if ap.exists():
        try:
            activity = json.loads(ap.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    parts: list[str] = []
    model = str(doc.get("model") or "")
    when = str(doc.get("created_at") or "")
    badge = '<span class="pill"><span class="dot"></span> processed on-device &middot; network not used</span>' \
        if doc.get("on_device") else '<span class="pill">server backend</span>'
    parts.append(f'<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">'
                 f'{badge}<span class="crumb">{_e(model)}</span><span class="crumb">{_e(when)}</span>'
                 f'<span style="flex:1"></span><a class="btn" href="/export/{sid}" target="_blank">Export report</a></div>')

    if _dget(rs, "total_tokens") is not None:
        try:
            parts.append(f'<div class="banner">{_e(cost_summary_line(rs))}</div>')
        except (KeyError, TypeError, ValueError):
            pass  # partial/mistyped run_stats: drop the banner, keep the page

    fail = _dget(rs, "clips_failed") or 0
    rt = _dget(rs, "realtime_factor")
    peak = _dget(rs, "peak_gb")
    parts.append(f"""
<div class="tiles">
  <div class="tile"><b>{_e(_dget(facts, "total_strokes", 0))}</b><span>strokes</span></div>
  <div class="tile"><b>{_e(_dget(facts, "clips_analyzed", 0))}</b><span>rallies analyzed</span></div>
  <div class="tile"><b>{_e(_dget(split, "near", 0))} / {_e(_dget(split, "far", 0))}</b><span>near / far</span></div>
  <div class="tile"><b>{_e(rt) if rt else "&mdash;"}&times;</b><span>vs realtime</span></div>
  <div class="tile"><b>{_e(peak) if peak else "&mdash;"}</b><span>peak GB</span>{f'<div class="sub">{_e(fail)} clip(s) skipped</div>' if fail else ''}</div>
</div>""")

    parts.append(_timeline(sid, doc, activity))

    # flagged moments (frames as URLs, not data URIs)
    def img_url(fp: Path) -> str | None:
        try:
            rel = fp.relative_to(sdir)
        except ValueError:
            return None
        return f"/frames/{sid}/{rel.as_posix()}"

    moments = collect_flag_moments(sdir, doc, img_fn=img_url)
    if moments:
        cards = []
        for m in moments:
            img = f'<img loading="lazy" src="{_e(m["img"])}" alt="flagged frame">' if m["img"] else \
                  '<div style="aspect-ratio:16/9;background:#000"></div>'
            cards.append(f"""
<div class="mcard">{img}<div class="b">{_sev_chip(m["severity"])}
  <span class="code">{_e(m["code"])}</span>
  <div class="mm">{_e(m["kind"])} &middot; {_e(m["stroke"])} ({_e(m["player"])}) &middot; t={m["t_s"]:.1f}s</div>
  <div class="ev">{_e(m["evidence"])}</div></div></div>""")
        parts.append(f'<div class="kicker">Flagged moments</div><div class="mgrid">{"".join(cards)}</div>')

    # rally cards
    parts.append('<div class="kicker">Rallies</div>')
    for c in _dget(doc, "clips") or []:
        if not isinstance(c, dict):
            continue
        idx = _e(c.get("index", "?"))
        a = c.get("analysis")
        if c.get("status") != "ok" or not isinstance(a, dict):
            parts.append(f'<div class="card" id="clip-{idx}"><div class="rally"><div>'
                         f'<div class="t">Clip {idx}</div>'
                         f'<div class="m">skipped &mdash; {_e(c.get("error", "no analysis"))}</div></div></div></div>')
            continue
        frames = _clip_frames(sdir, c)
        thumbs = frames[:: max(1, len(frames) // 3)][:3]
        # img_url guards against frame paths escaping the session dir
        urls = [u for u in (img_url(t) for t in thumbs) if u]
        timg = "".join(f'<img loading="lazy" src="{_e(u)}" alt="frame">' for u in urls)
        n_flags = sum(len(s.get("technique_flags") or []) + len(s.get("tactical_flags") or [])
                      for s in a.get("strokes", []) if isinstance(s, dict))
        parts.append(f"""
<div class="card" id="clip-{idx}"><div class="rally">
  <div class="fr">{timg}</div>
  <div><div class="t">Clip {idx} <span class="m">{_num(a.get("start_s")):.1f}&ndash;{_num(a.get("end_s")):.1f}s</span></div>
    <div class="m">{len(a.get("strokes", []))} strokes &middot; {n_flags} flags &middot; confidence {_e(a.get("confidence", "?"))}</div>
    <p>{_e(a.get("rally_summary", ""))}</p></div>
</div></div>""")

    # coaching report
    md = sdir / "session_report.md"
    if md.exists():
        parts.append('<div class="kicker">Coaching report</div>')
        parts.append(f'<div class="card rep">{_md_to_html(md.read_text())}</div>')

    return _page(ref.title, "".join(parts), crumb="session")


# ---------------- run progress page ----------------

def render_run_page(run) -> str:
    body = f"""
<div class="card">
  <div class="status-row" id="strow"><span class="spin" id="spin"></span>
    <b id="stword">running</b><span class="crumb" id="elapsed"></span>
    <span style="flex:1"></span><span class="pill">{_e(run.video)}</span></div>
  <div class="console" id="log">starting&hellip;</div>
  <p class="hint" id="donebar" style="display:none;margin:12px 0 0">
    <a class="btn btn-pri" id="openlink" href="/">Open session</a></p>
</div>
<script>
var rid = {json.dumps(run.rid)};
function esc(s){{ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }}
function poll(){{
  fetch('/api/runs/'+rid).then(function(r){{
    if (!r.ok) throw new Error('gone');
    return r.json();
  }}).then(function(d){{
    if (d.error || !Array.isArray(d.log)) throw new Error('bad payload');
    fails = 0;
    var log = document.getElementById('log');
    log.innerHTML = d.log.map(esc).join('\\n');
    log.scrollTop = log.scrollHeight;
    document.getElementById('elapsed').textContent = d.elapsed_s.toFixed(0)+'s';
    if (d.status === 'running') {{ setTimeout(poll, 1200); return; }}
    document.getElementById('spin').style.display = 'none';
    document.getElementById('stword').textContent = d.status === 'done' ? 'complete' : 'failed';
    var a = document.getElementById('openlink');
    document.getElementById('donebar').style.display = 'block';
    if (d.status === 'done' && d.session) {{
      a.href = '/session/' + d.session;
      setTimeout(function(){{ location.href = a.href; }}, 1500);
    }} else if (d.status === 'done') {{
      a.textContent = 'Back to dashboard';   // e.g. a plumbing-only dry run
    }} else {{
      document.getElementById('stword').style.color = 'var(--chip-critical)';
      a.textContent = 'Back to dashboard';
    }}
  }}).catch(function(){{
    if (++fails >= 5) {{
      document.getElementById('spin').style.display = 'none';
      var w = document.getElementById('stword');
      w.textContent = 'connection lost'; w.style.color = 'var(--chip-critical)';
      document.getElementById('donebar').style.display = 'block';
      document.getElementById('openlink').textContent = 'Back to dashboard';
      return;
    }}
    setTimeout(poll, 2000);
  }});
}}
var fails = 0;
poll();
</script>"""
    return _page("Analyzing", body, crumb=run.video)
