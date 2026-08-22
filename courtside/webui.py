"""Views for courtside-ui: the SaaS-style dashboard, session, and run pages.

Pure functions dict-in/HTML-out so they render (and test) without a server.
The design system is deliberately restrained and premium: one interactive
accent, a tennis-ball brand spark, status colors that carry severity (always
with a text label, never color alone), tabular figures for metrics, and a
light/dark palette from one token set with a viewer toggle that wins over the
OS setting in both directions.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from . import config
from .report import cost_summary_line
from .report_html import _clip_frames, _md_to_html, collect_flag_moments

# severity -> (color, label). Labels always accompany the color.
SEV = {
    "high":   ("#e5484d", "critical"),
    "medium": ("#f5a623", "warning"),
    "low":    ("#8a8f98", "minor"),
}


def _e(x: Any) -> str:
    return html.escape(str(x), quote=True)


def _num(x) -> float:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0.0


def _dget(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else default


# ---------------- design system ----------------

_CSS = """
:root{
  color-scheme: light;
  --page:#f5f6f8; --page-2:#eceef2; --surface:#ffffff; --surface-2:#f7f8fa;
  --ink:#0b0d12; --ink-2:#4a4f5a; --muted:#878d99;
  --border:rgba(11,13,18,.09); --border-2:rgba(11,13,18,.06);
  --accent:#2563eb; --accent-2:#1d4ed8; --accent-ink:#ffffff; --accent-wash:rgba(37,99,235,.08);
  --spark:#7bb500; --good:#16a34a; --good-ink:#0f7a34; --warn:#f5a623; --crit:#e5484d;
  --console:#0e1013; --console-ink:#cfd3da; --console-dim:#7b818c;
  --shadow:0 1px 2px rgba(11,13,18,.05),0 8px 24px rgba(11,13,18,.06);
  --shadow-lg:0 2px 4px rgba(11,13,18,.06),0 20px 48px rgba(11,13,18,.12);
  --glow:radial-gradient(60% 120% at 15% 0%,rgba(37,99,235,.10),transparent 60%);
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --page:#08090c; --page-2:#0c0e12; --surface:#111318; --surface-2:#151821;
  --ink:#f3f5f8; --ink-2:#aab1bd; --muted:#6b7280;
  --border:rgba(255,255,255,.08); --border-2:rgba(255,255,255,.05);
  --accent:#5b8cf0; --accent-2:#6f9bf5; --accent-ink:#0a0c10; --accent-wash:rgba(91,140,240,.12);
  --spark:#c8f647; --good:#3ec77a; --good-ink:#4ade80; --warn:#f5b942; --crit:#f26569;
  --console:#0a0c0f; --console-ink:#cdd2da; --console-dim:#6b727d;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 30px rgba(0,0,0,.45);
  --shadow-lg:0 2px 6px rgba(0,0,0,.5),0 24px 60px rgba(0,0,0,.6);
  --glow:radial-gradient(60% 120% at 15% 0%,rgba(91,140,240,.16),transparent 60%);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    color-scheme: dark;
    --page:#08090c; --page-2:#0c0e12; --surface:#111318; --surface-2:#151821;
    --ink:#f3f5f8; --ink-2:#aab1bd; --muted:#6b7280;
    --border:rgba(255,255,255,.08); --border-2:rgba(255,255,255,.05);
    --accent:#5b8cf0; --accent-2:#6f9bf5; --accent-ink:#0a0c10; --accent-wash:rgba(91,140,240,.12);
    --spark:#c8f647; --good:#3ec77a; --good-ink:#4ade80; --warn:#f5b942; --crit:#f26569;
    --console:#0a0c0f; --console-ink:#cdd2da; --console-dim:#6b727d;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 30px rgba(0,0,0,.45);
    --shadow-lg:0 2px 6px rgba(0,0,0,.5),0 24px 60px rgba(0,0,0,.6);
    --glow:radial-gradient(60% 120% at 15% 0%,rgba(91,140,240,.16),transparent 60%);
  }
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--page);color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:none;opacity:.85}
::selection{background:var(--accent-wash)}
.tnum{font-variant-numeric:tabular-nums;letter-spacing:-.01em}

/* shell */
.app{display:flex;min-height:100vh}
.side{width:230px;flex:0 0 230px;border-right:1px solid var(--border);background:var(--surface);
  padding:20px 14px;display:flex;flex-direction:column;gap:3px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16.5px;
  letter-spacing:-.02em;margin:4px 8px 20px;color:var(--ink)}
.brand .ball{width:16px;height:16px;border-radius:50%;flex:0 0 16px;position:relative;
  background:radial-gradient(circle at 35% 30%,#e4ff86,var(--spark) 60%,#8fbf05);
  box-shadow:0 0 0 1px rgba(0,0,0,.06),0 2px 6px rgba(150,190,10,.4)}
.brand .ball::after{content:"";position:absolute;inset:0;border-radius:50%;
  background:repeating-linear-gradient(120deg,transparent 0 6px,rgba(255,255,255,.35) 6px 7px)}
.nav{display:flex;flex-direction:column;gap:2px}
.nav a{display:flex;align-items:center;gap:10px;padding:9px 11px;border-radius:9px;
  color:var(--ink-2);font-weight:500;transition:background .12s,color .12s}
.nav a:hover{background:var(--surface-2);color:var(--ink);opacity:1}
.nav a.on{background:var(--accent-wash);color:var(--accent)}
.nav svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.7;flex:0 0 17px}
.side .foot{margin-top:auto;display:flex;flex-direction:column;gap:8px;padding:8px}
.privacy{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--ink-2);
  background:var(--surface-2);border:1px solid var(--border-2);border-radius:10px;padding:9px 11px}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--good);flex:0 0 8px;position:relative}
.pulse::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:1px solid var(--good);
  opacity:.5;animation:pulse 2s ease-out infinite}
@keyframes pulse{0%{transform:scale(.6);opacity:.7}100%{transform:scale(1.6);opacity:0}}

.main{flex:1;min-width:0}
.wrap{max-width:1120px;margin:0 auto;padding:26px 34px 90px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:6px}
.topbar h1{font-size:20px;margin:0;letter-spacing:-.02em}
.crumb{color:var(--muted);font-weight:500}
.iconbtn{border:1px solid var(--border);background:var(--surface);color:var(--ink-2);width:34px;height:34px;
  border-radius:9px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;font-size:15px}
.iconbtn:hover{background:var(--surface-2);color:var(--ink)}

/* buttons */
button,.btn{border:1px solid var(--border);background:var(--surface);color:var(--ink);border-radius:9px;
  padding:8px 14px;font:inherit;font-weight:560;cursor:pointer;transition:transform .08s,background .12s,border-color .12s;
  display:inline-flex;align-items:center;gap:7px}
button:hover,.btn:hover{background:var(--surface-2)}
button:active,.btn:active{transform:translateY(1px)}
.btn-pri{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn-pri:hover{background:var(--accent-2);border-color:var(--accent-2)}
.btn-danger{color:var(--crit);border-color:color-mix(in srgb,var(--crit) 40%,var(--border))}
.btn-danger:hover{background:color-mix(in srgb,var(--crit) 10%,var(--surface))}
.btn svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.8}

/* hero */
.hero{position:relative;overflow:hidden;border:1px solid var(--border);border-radius:18px;
  background:var(--surface);background-image:var(--glow);padding:26px 28px;margin:14px 0 18px;box-shadow:var(--shadow)}
.hero .eyebrow{font-size:12px;font-weight:650;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.hero h2{font-size:26px;line-height:1.15;letter-spacing:-.025em;margin:8px 0 6px;max-width:640px}
.hero p{color:var(--ink-2);margin:0;max-width:600px;font-size:14.5px}
.pillars{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px}
.pillar{border:1px solid var(--border-2);border-radius:13px;background:var(--surface-2);padding:15px 16px}
.pillar .v{font-size:24px;font-weight:700;letter-spacing:-.02em;line-height:1.1}
.pillar .k{font-size:12px;color:var(--ink-2);margin-top:3px;font-weight:550}
.pillar .d{font-size:11.5px;color:var(--muted);margin-top:2px}

/* cards + grids */
.card{background:var(--surface);border:1px solid var(--border);border-radius:15px;padding:20px;
  margin-bottom:18px;box-shadow:var(--shadow)}
.card h2{margin:0 0 3px;font-size:15.5px;letter-spacing:-.01em}
.card .hint{color:var(--muted);font-size:12.5px;margin:0 0 14px}
.kicker{font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
  font-weight:650;margin:28px 4px 12px;display:flex;align-items:center;gap:8px}
.kicker .rule{flex:1;height:1px;background:var(--border)}

.banner{display:flex;align-items:center;gap:10px;border:1px solid var(--border);
  border-left:3px solid var(--accent);background:var(--surface);border-radius:11px;
  padding:12px 15px;margin-bottom:16px;font-size:13.5px;box-shadow:var(--shadow)}
.banner.err{border-left-color:var(--crit)}
.banner>svg{width:16px;height:16px;flex:0 0 16px;stroke:currentColor;fill:none;stroke-width:1.8;color:var(--accent)}
.banner.err>svg{color:var(--crit)}
.banner .sp{flex:1}

/* stat tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:0 0 4px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:13px;padding:15px 16px;box-shadow:var(--shadow)}
.tile b{display:block;font-size:25px;font-weight:700;letter-spacing:-.025em;line-height:1.15}
.tile span{color:var(--ink-2);font-size:12px;font-weight:500}
.tile .sub{color:var(--muted);font-size:11px;margin-top:3px}
.tile.accent b{color:var(--accent)}

/* form */
form.analyze{display:grid;grid-template-columns:1fr 1fr;gap:14px}
form.analyze .full{grid-column:1/-1}
label.f{display:block;font-size:12px;color:var(--ink-2);margin-bottom:5px;font-weight:560}
input[type=text],select{width:100%;border:1px solid var(--border);background:var(--surface-2);color:var(--ink);
  border-radius:10px;padding:10px 12px;font:inherit;transition:border-color .12s,box-shadow .12s}
input[type=text]:focus,select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-wash)}
input::placeholder{color:var(--muted)}
.checks{display:flex;gap:16px;flex-wrap:wrap;align-items:center;color:var(--ink-2);font-size:13px}
.checks label{display:inline-flex;gap:7px;align-items:center;cursor:pointer;user-select:none}
.checks input{accent-color:var(--accent);width:15px;height:15px}
.cta-row{display:flex;align-items:center;gap:14px}
.cta-row .note{color:var(--muted);font-size:12px}

/* session grid */
.sess{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.scard{background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden;
  display:block;color:var(--ink);box-shadow:var(--shadow);transition:transform .12s,border-color .12s,box-shadow .12s}
.scard:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--accent) 40%,var(--border));
  box-shadow:var(--shadow-lg);opacity:1}
.scard .strip{display:flex;gap:0;height:96px;background:var(--surface-2)}
.scard .strip img{flex:1;min-width:0;object-fit:cover;height:100%;border-right:1px solid var(--border-2)}
.scard .strip .ph{flex:1;background:repeating-linear-gradient(135deg,var(--surface-2) 0 10px,var(--page-2) 10px 20px)}
.scard .b{padding:13px 15px}
.scard .t{font-weight:620;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.scard .m{color:var(--muted);font-size:12px;margin-top:2px}
.scard .row{display:flex;gap:16px;margin-top:11px;font-size:12.5px;color:var(--ink-2)}
.scard .row b{font-weight:680;color:var(--ink)}
.tag{font-size:10.5px;font-weight:650;letter-spacing:.03em;text-transform:uppercase;
  padding:2px 7px;border-radius:6px;background:var(--surface-2);color:var(--muted);border:1px solid var(--border-2)}
.empty{text-align:center;padding:46px 20px;color:var(--muted)}
.empty .big{font-size:16px;color:var(--ink-2);font-weight:560;margin-bottom:6px}

/* chips */
.chip{display:inline-flex;align-items:center;gap:5px;border-radius:6px;padding:2px 8px;
  font-size:11px;font-weight:650;color:#fff;letter-spacing:.01em}
.chip svg{width:9px;height:9px;fill:currentColor}
.chip.minor{color:var(--ink);background:var(--surface-2)!important;border:1px solid var(--border)}
.badge{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--border);border-radius:999px;
  padding:5px 12px;font-size:12px;color:var(--ink-2);background:var(--surface);font-weight:500}
.badge.on{color:var(--good-ink);border-color:color-mix(in srgb,var(--good) 35%,var(--border))}

/* timeline */
.timeline-wrap{overflow-x:auto;margin:2px -2px 0}
.legend{display:flex;gap:16px;align-items:center;font-size:12px;color:var(--ink-2);margin-top:10px;flex-wrap:wrap}
.legend .sw{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px;vertical-align:-1px}

/* flagged moments */
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(238px,1fr));gap:13px}

/* deep-dive coaching cards */
.dcard{background:var(--surface);border:1px solid var(--border);border-radius:15px;
  overflow:hidden;margin-bottom:18px;box-shadow:var(--shadow)}
.dcard .media{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
  gap:1px;background:var(--border-2)}
.dcard .media>div{background:#000;position:relative}
.dcard .media video,.dcard .media img{width:100%;display:block;aspect-ratio:16/9;object-fit:contain;background:#000}
.dcard .medialbl{position:absolute;top:8px;left:8px;font-size:10.5px;font-weight:650;
  letter-spacing:.04em;text-transform:uppercase;color:#fff;background:rgba(0,0,0,.55);
  padding:2px 8px;border-radius:6px}
.dcard .body{padding:16px 18px}
.dcard .hd{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:9px}
.dcard .hd .code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:650;font-size:14px}
.dcard .hd .mm{color:var(--muted);font-size:12px}
.anglerow{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 12px}
.angle{border:1px solid var(--border);background:var(--surface-2);border-radius:8px;
  padding:4px 10px;font-size:12px;color:var(--ink-2)}
.angle b{color:var(--ink);font-weight:650}
.cgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-top:4px}
.cbox .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  font-weight:650;margin-bottom:4px}
.cbox p{margin:0;font-size:13px;color:var(--ink-2);line-height:1.5}
.target{border-left:3px solid var(--good);background:color-mix(in srgb,var(--good) 7%,var(--surface));
  border-radius:8px;padding:9px 13px;margin-top:13px;font-size:13px}
.target b{color:var(--good-ink)}
.drill{border:1px dashed var(--border);border-radius:10px;padding:11px 14px;margin-top:11px;font-size:13px}
.drill .nm{font-weight:650}
.drill .m{color:var(--muted);font-size:12px}
.mcard{background:var(--surface);border:1px solid var(--border);border-radius:13px;overflow:hidden;box-shadow:var(--shadow)}
.mcard img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:#000}
.mcard .b{padding:11px 13px}
.mcard .code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;font-weight:600;margin-left:6px}
.mcard .ev{color:var(--ink-2);font-size:12.5px;margin-top:6px;line-height:1.45}
.mcard .mm{color:var(--muted);font-size:11.5px;margin-top:4px}

/* rally cards */
.rally{display:flex;gap:15px;align-items:flex-start}
.rally .fr{display:flex;gap:6px;flex:0 0 auto}
.rally .fr img{width:108px;aspect-ratio:16/9;object-fit:cover;border-radius:7px;border:1px solid var(--border);display:block}
.rally .t{font-weight:620}
.rally .m{color:var(--muted);font-size:12px}
.rally p{margin:7px 0 0;color:var(--ink-2);font-size:13px;line-height:1.5}

/* report prose */
.rep{max-width:720px}
.rep h1{font-size:19px;letter-spacing:-.01em;border-bottom:1px solid var(--border);padding-bottom:9px}
.rep h2{font-size:15px;margin-top:24px}
.rep li{margin:5px 0}
.rep code{background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:12.5px}
.rep hr{border:0;border-top:1px solid var(--border);margin:20px 0}
.muted{color:var(--muted)}

/* run / progress page */
.prog{margin:2px 0 18px}
.progbar{height:9px;border-radius:99px;background:var(--surface-2);border:1px solid var(--border-2);overflow:hidden}
.progbar .fill{height:100%;width:4%;border-radius:99px;background:linear-gradient(90deg,var(--accent),var(--accent-2));
  transition:width .5s cubic-bezier(.2,.7,.2,1)}
.progbar.failed .fill{background:var(--crit)}
.phases{display:flex;justify-content:space-between;margin-top:14px;gap:6px}
.phase{flex:1;text-align:center;font-size:11.5px;color:var(--muted);position:relative;font-weight:550}
.phase .dot{width:11px;height:11px;border-radius:50%;background:var(--surface-2);border:2px solid var(--border);
  margin:0 auto 7px;transition:background .2s,border-color .2s}
.phase.done .dot{background:var(--good);border-color:var(--good)}
.phase.active .dot{background:var(--accent);border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-wash)}
.phase.active{color:var(--ink)}
.phase.done{color:var(--ink-2)}
.status-row{display:flex;gap:11px;align-items:center;margin-bottom:16px}
.status-row .big{font-size:17px;font-weight:650;letter-spacing:-.01em}
.spin{width:15px;height:15px;border:2px solid var(--border);border-top-color:var(--accent);
  border-radius:50%;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.console{background:var(--console);color:var(--console-ink);border-radius:12px;padding:15px 17px;
  font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;min-height:170px;max-height:440px;
  overflow-y:auto;white-space:pre-wrap;word-break:break-word;border:1px solid var(--border)}
.console-h{display:flex;align-items:center;justify-content:space-between;margin:0 2px 8px}
.console-h .lbl{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:650}

#tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--border);
  border-radius:9px;padding:8px 11px;font-size:12px;color:var(--ink);box-shadow:var(--shadow-lg);
  opacity:0;transition:opacity .1s;z-index:60;max-width:270px}
#tip .h{font-weight:650}
#tip .s{color:var(--muted)}

@media (max-width:820px){
  .app{flex-direction:column}
  .side{width:auto;height:auto;position:static;flex-direction:row;align-items:center;flex-wrap:wrap;padding:12px 14px}
  .side .foot,.brand{margin:0}.nav{flex-direction:row}.nav svg{display:none}.side .foot{display:none}
  .wrap{padding:20px 18px 70px}.pillars{grid-template-columns:1fr}form.analyze{grid-template-columns:1fr}
}
"""

_JS = """
(function(){var t=localStorage.getItem('cs-theme');if(t)document.documentElement.dataset.theme=t;})();
function toggleTheme(){
  var cur=document.documentElement.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
  var next=cur==='dark'?'light':'dark';
  document.documentElement.dataset.theme=next;localStorage.setItem('cs-theme',next);
}
document.addEventListener('DOMContentLoaded',function(){
  var tip=document.createElement('div');tip.id='tip';document.body.appendChild(tip);
  document.querySelectorAll('[data-tip]').forEach(function(el){
    el.addEventListener('mousemove',function(ev){
      tip.innerHTML=el.dataset.tip;tip.style.opacity=1;
      var x=Math.min(ev.clientX+14,innerWidth-tip.offsetWidth-8);
      var y=Math.min(ev.clientY+14,innerHeight-tip.offsetHeight-8);
      tip.style.left=x+'px';tip.style.top=y+'px';
    });
    el.addEventListener('mouseleave',function(){tip.style.opacity=0;});
  });
});
"""

_ICONS = {
    "dash": '<svg viewBox="0 0 24 24"><path d="M3 12h6V3H3zM15 21h6v-9h-6zM3 21h6v-5H3zM15 8h6V3h-6z"/></svg>',
    "new": '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
    "doc": '<svg viewBox="0 0 24 24"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8zM14 3v5h5"/></svg>',
    "download": '<svg viewBox="0 0 24 24"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>',
    "stop": '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
    "bolt": '<svg viewBox="0 0 24 24"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>',
}

_FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
            "%3Ccircle cx='8' cy='8' r='7' fill='%23c8f647'/%3E"
            "%3Cpath d='M2 4 Q8 8 2 12 M14 4 Q8 8 14 12' stroke='white' stroke-width='1.2' fill='none'/%3E%3C/svg%3E")


def _page(title: str, body: str, active: str = "", crumb: str = "") -> str:
    crumb_html = f'<span class="crumb">{_e(crumb)}</span>' if crumb else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} - Courtside</title>
<link rel="icon" href="{_FAVICON}">
<style>{_CSS}</style><script>{_JS}</script></head>
<body><div class="app">
<aside class="side">
  <div class="brand"><span class="ball"></span> Courtside</div>
  <nav class="nav">
    <a href="/" class="{'on' if active == 'dash' else ''}">{_ICONS['dash']} Dashboard</a>
    <a href="/#new" class="{'on' if active == 'new' else ''}">{_ICONS['new']} New analysis</a>
    <a href="/#sessions">{_ICONS['doc']} Sessions</a>
  </nav>
  <div class="foot">
    <div class="privacy"><span class="pulse"></span> On-device &middot; 0 bytes uploaded</div>
  </div>
</aside>
<main class="main"><div class="wrap">
<div class="topbar"><h1>{_e(title)} {crumb_html}</h1>
  <button class="iconbtn" onclick="toggleTheme()" title="Toggle light / dark" aria-label="toggle theme">&#9681;</button>
</div>
{body}
</div></main></div></body></html>"""


def _sev_chip(severity: str) -> str:
    color, label = SEV.get(severity, SEV["low"])
    cls = "chip minor" if severity == "low" else "chip"
    style = "" if severity == "low" else f'style="background:{color}"'
    icon = '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'
    return f'<span class="{cls}" {style}>{icon}{label}</span>'


def _fmt_min(seconds: float) -> str:
    return f"{seconds / 60:.0f}"


# ---------------- dashboard ----------------

def _sparkline(values: list, w: int = 200, h: int = 44, suffix: str = "") -> str:
    """SVG sparkline; None entries (sessions without that metric) leave gaps."""
    pts = [(i, _num(v)) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return ""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    pad = 5

    def X(i: float) -> float:
        return pad + (w - 2 * pad) * (i - xs[0]) / max(1, xs[-1] - xs[0])

    def Y(v: float) -> float:
        return h - pad - (h - 2 * pad) * (v - lo) / span

    poly = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in pts)
    lx, lv = pts[-1]
    label = f"{lv:.0f}" if lv == int(lv) or abs(lv) >= 10 else f"{lv:.1f}"
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="trend">'
            f'<polyline points="{poly}" fill="none" stroke="var(--accent)" stroke-width="2"/>'
            f'<circle cx="{X(lx):.1f}" cy="{Y(lv):.1f}" r="3" fill="var(--accent)"/></svg>'
            f'<b class="tnum" style="font-size:13px">{label}{suffix}</b>')


def _trends_panel(real: list) -> str:
    """Cross-session trends: the 'is the player improving?' card."""
    refs = sorted(real, key=lambda r: str(_dget(r.doc, "created_at") or ""))
    if len(refs) < 2:
        return ""
    ideal: list = []
    err_rate: list = []
    fps_: list = []
    tech_counters: list[dict] = []
    for r in refs:
        f = _dget(r.doc, "facts") or {}
        cq = _dget(f, "contact_quality") or {}
        ideal.append(_num(cq.get("pct_ideal")) if isinstance(cq, dict) and cq.get("pct_ideal") is not None else None)
        oc = _dget(f, "outcomes") or {}
        err_rate.append(_num(oc.get("error_rate_pct"))
                        if isinstance(oc, dict) and oc.get("error_rate_pct") is not None else None)
        total = _num(_dget(f, "total_strokes"))
        nf = 0.0
        merged: dict[str, float] = {}
        for key in ("top_technique_flags", "top_tactical_flags"):
            counts = _dget(f, key) or {}
            if isinstance(counts, dict):
                for k, v in counts.items():
                    merged[k] = merged.get(k, 0) + _num(v)
                    nf += _num(v)
        tech_counters.append(merged)
        fps_.append(round(nf / total, 2) if total else None)

    rows: list[str] = []
    for label, vals, suffix, better in (
            ("ideal contact", ideal, "%", "higher"),
            ("error rate (AI-called)", err_rate, "%", "lower"),
            ("flags per stroke", fps_, "", "lower")):
        spark = _sparkline(vals, suffix=suffix)
        if not spark:
            continue
        rows.append(f'<div style="display:flex;gap:14px;align-items:center;padding:6px 0;'
                    f'border-bottom:1px solid var(--border-2)">'
                    f'<span style="flex:0 0 190px;font-size:13px">{_e(label)}'
                    f'<span class="muted" style="display:block;font-size:11px">{better} is better</span></span>'
                    f'<span style="display:flex;gap:10px;align-items:center">{spark}</span></div>')

    # top recurring flags across sessions, with direction vs the earlier mean
    totals: dict[str, float] = {}
    for c in tech_counters:
        for k, v in c.items():
            totals[k] = totals.get(k, 0) + v
    flag_rows: list[str] = []
    for code, _tot in sorted(totals.items(), key=lambda kv: -kv[1])[:3]:
        series = [c.get(code, 0) for c in tech_counters]
        prior = series[:-1]
        mean_prior = sum(prior) / len(prior) if prior else 0
        arrow = ("&darr;" if series[-1] < mean_prior else
                 ("&uarr;" if series[-1] > mean_prior else "&rarr;"))
        good = series[-1] <= mean_prior
        cells = "".join(f'<span class="tnum" style="width:26px;text-align:center">{int(n)}</span>'
                        for n in series[-8:])
        flag_rows.append(f'<div style="display:flex;gap:8px;align-items:center;font-size:12.5px;'
                         f'padding:4px 0;border-bottom:1px solid var(--border-2)">'
                         f'<span style="flex:1">{_e(code.replace("_", " "))}</span>{cells}'
                         f'<span style="width:20px;text-align:center;'
                         f'color:{"var(--good)" if good else "var(--crit)"}">{arrow}</span></div>')

    if not rows and not flag_rows:
        return ""
    flags_block = (f'<div style="flex:1;min-width:260px"><div class="kicker" style="margin:0 0 6px">'
                   f'Most recurrent flags (per session, oldest &rarr; newest)</div>{"".join(flag_rows)}</div>'
                   if flag_rows else "")
    return (f'<div class="card"><h2>Trends &mdash; last {len(refs)} sessions</h2>'
            f'<p class="hint">Session-over-session movement. Sessions analyzed before outcome '
            f'tracking show gaps.</p><div style="display:flex;gap:26px;flex-wrap:wrap">'
            f'<div style="flex:1;min-width:300px">{"".join(rows)}</div>{flags_block}</div></div>')


def render_dashboard(state, error: str | None = None) -> str:
    sessions = sorted(state.sessions(max_age_s=0).values(),
                      key=lambda r: str(_dget(r.doc, "created_at") or ""), reverse=True)
    real = [r for r in sessions if not getattr(r, "is_sample", False)]
    videos = state.videos()

    # aggregate counters reflect real work; until any exists, fall back to all
    # sessions so a fresh install (just the sample) shows a populated dashboard
    agg = real if real else sessions
    n_strokes = int(sum(_num(_dget(_dget(r.doc, "facts") or {}, "total_strokes")) for r in agg))
    n_flags = 0
    for r in agg:
        f = _dget(r.doc, "facts") or {}
        for key in ("top_technique_flags", "top_tactical_flags"):
            counts = _dget(f, key) or {}
            if isinstance(counts, dict):
                n_flags += int(sum(_num(v) for v in counts.values()))
    minutes = sum(_num(_dget(r.doc, "video_duration_s")) for r in agg)

    # hero pillars can draw on any session (sample included, as illustration)
    rts = [_num(_dget(_dget(r.doc, "run_stats") or {}, "realtime_factor")) for r in sessions]
    rts = [x for x in rts if x > 0]
    best_rt = max(rts) if rts else 0
    clouds = [_num(_dget(_dget(r.doc, "run_stats") or {}, "cloud_equiv_usd")) for r in sessions]
    clouds = [c for c in clouds if c > 0]
    cloud_ex = max(clouds) if clouds else 0

    parts: list[str] = []
    if error:
        parts.append(f'<div class="banner err">{_ICONS["bolt"]}<span>{_e(error)}</span></div>')
    active = state.runs.active()
    if active:
        parts.append(f'<div class="banner">{_ICONS["bolt"]}<span>Analyzing <b>{_e(active.video)}</b></span>'
                     f'<span class="sp"></span><a class="btn" href="/run/{_e(active.rid)}">Watch progress</a></div>')

    # hero
    rt_v = f"{best_rt:.0f}&times;" if best_rt >= 1 else "Faster"
    rt_d = "faster than watching it" if best_rt else "than realtime"
    cloud_d = f"vs ~${cloud_ex:.2f}/session on a frontier API" if cloud_ex else "no per-minute cloud bill"
    parts.append(f"""
<div class="hero">
  <div class="eyebrow">Error tracking for coaches</div>
  <h2>Every error, mapped &mdash; the courtside error chart, filled in from video automatically.</h2>
  <p>Courtside skips the dead time, finds every stroke, and charts each error by court zone, runway,
     and strike zone &mdash; with the video evidence attached. Built to support the coach's eye,
     never to replace it. No clips leave the device.</p>
  <div class="pillars">
    <div class="pillar"><div class="v">100% on-device</div><div class="k">Private by architecture</div>
      <div class="d">footage never leaves the laptop</div></div>
    <div class="pillar"><div class="v tnum">{rt_v}</div><div class="k">{rt_d}</div>
      <div class="d">measured end-to-end wall clock</div></div>
    <div class="pillar"><div class="v">$0 cloud</div><div class="k">Zero marginal cost</div>
      <div class="d">{cloud_d}</div></div>
  </div>
</div>""")

    # aggregate tiles (real work only)
    parts.append(f"""
<div class="tiles">
  <div class="tile"><b class="tnum">{len(agg)}</b><span>sessions analyzed</span></div>
  <div class="tile"><b class="tnum">{_fmt_min(minutes)}</b><span>minutes of footage</span></div>
  <div class="tile"><b class="tnum">{n_strokes}</b><span>strokes read</span></div>
  <div class="tile"><b class="tnum">{n_flags}</b><span>coaching flags</span></div>
</div>""")

    if len(real) >= 2:
        trends = _trends_panel(real)
        if trends:
            parts.append(trends)

    # new analysis
    vid_opts = "".join(f'<option value="{_e(v)}">{_e(Path(v).name)}</option>' for v in videos)
    model_opts = "".join(
        f'<option value="{_e(k)}"{" selected" if k == config.DEFAULT_MODEL_KEY else ""}>'
        f'{_e(k)} &mdash; ~{spec.approx_weights_gb:.0f} GB</option>'
        for k, spec in config.MODELS.items())
    parts.append(f"""
<div class="card" id="new">
  <h2>New analysis</h2>
  <p class="hint">Runs entirely on this machine. Choose a workspace video, paste a path, or drop in a YouTube link
     &mdash; the download fetches over the network, the footage stays on the device.</p>
  <form class="analyze" method="post" action="/analyze">
    <div><label class="f" for="vsel">Video in workspace</label>
      <select id="vsel" onchange="if(this.value)document.getElementById('vpath').value=this.value">
        <option value="">&mdash; choose &mdash;</option>{vid_opts}</select></div>
    <div><label class="f" for="vpath">&hellip;or path / YouTube URL</label>
      <input type="text" id="vpath" name="video"
             placeholder="/path/to/match.mp4  or  https://youtube.com/watch?v=..."></div>
    <div><label class="f" for="model">Model</label>
      <select id="model" name="model">{model_opts}</select></div>
    <div style="align-self:end"><label class="f">Options</label>
      <div class="checks" style="padding:9px 0 0">
        <label><input type="checkbox" name="quick" value="1"> quick test (first 3 rallies only)</label>
        <label><input type="checkbox" name="offline" value="1"> offline</label>
        <label><input type="checkbox" name="dry_run" value="1"> plumbing only</label>
      </div></div>
    <div class="full" style="border-top:1px solid var(--border-2);padding-top:13px">
      <div class="checks" style="margin-bottom:9px">
        <label><input type="checkbox" name="use_openrouter" value="1"
          onchange="document.getElementById('cloudrow').style.display=this.checked?'block':'none'">
          use a cloud model via OpenRouter</label>
        <span class="note" style="color:var(--warn)">frames are uploaded to OpenRouter for that run
          &mdash; not on-device</span>
      </div>
      <div id="cloudrow" style="display:none;max-width:420px">
        <label class="f" for="cloud_model">OpenRouter model</label>
        <input type="text" id="cloud_model" name="cloud_model" value="qwen/qwen2.5-vl-72b-instruct"
               list="cloud_models">
        <datalist id="cloud_models">
          <option value="qwen/qwen2.5-vl-72b-instruct">proven default</option>
          <option value="qwen/qwen3.8-27b">newer generation, image+video input, ~same cost (unproven on tennis)</option>
          <option value="google/gemini-2.5-flash">balanced cost + accuracy</option>
          <option value="google/gemini-2.5-pro">high accuracy</option>
          <option value="anthropic/claude-sonnet-4.5">high accuracy</option>
        </datalist>
        <div class="note" style="margin-top:5px">stronger models call more stroke outcomes
          (net/long/wide) and read the far player better &mdash; at higher per-match cost</div>
        <div class="note" style="margin-top:5px">needs <code>OPENROUTER_API_KEY</code> set when
          starting <code>courtside-ui</code></div>
      </div>
    </div>
    <div class="full cta-row"><button class="btn-pri" type="submit">{_ICONS['bolt']} Analyze</button>
      <span class="note">Quick pass analyzes the first 3 rallies for a fast first result.</span></div>
  </form>
</div>""")

    # sessions
    parts.append('<div class="kicker" id="sessions">Sessions<span class="rule"></span></div>')
    if not sessions:
        parts.append('<div class="card"><div class="empty"><div class="big">No sessions yet</div>'
                     'Run your first analysis above &mdash; or open the bundled sample to see the output.</div></div>')
    else:
        cards = "".join(_session_card(state, r) for r in sessions)
        parts.append(f'<div class="sess">{cards}</div>')

    return _page("Dashboard", "".join(parts), active="dash")


def _session_card(state, r) -> str:
    facts = _dget(r.doc, "facts") or {}
    rs = _dget(r.doc, "run_stats") or {}
    model = str(_dget(r.doc, "model") or "").split("/")[-1]
    when = str(_dget(r.doc, "created_at") or "")[:10]
    clips = _dget(r.doc, "clips") or []
    n_clip = _dget(facts, "clips_analyzed", len(clips) if isinstance(clips, list) else 0)
    rt = _dget(rs, "realtime_factor")

    # thumbnail strip: first frame of up to 4 ok clips
    thumbs = []
    for c in (clips if isinstance(clips, list) else []):
        if isinstance(c, dict) and c.get("analysis"):
            frames = _clip_frames(r.dir, c)
            if frames:
                rel = frames[0].name
                fd = c.get("frame_dir", f"clip_{c.get('index', 0):03d}")
                thumbs.append(f"/frames/{r.sid}/{fd}/{rel}")
        if len(thumbs) >= 4:
            break
    strip = "".join(f'<img loading="lazy" src="{_e(u)}" alt="">' for u in thumbs)
    if len(thumbs) < 4:
        strip += "".join('<div class="ph"></div>' for _ in range(4 - len(thumbs)))
    sample = '<span class="tag">Sample</span>' if getattr(r, "is_sample", False) else ""

    return f"""
<a class="scard" href="/session/{_e(r.sid)}">
  <div class="strip">{strip}</div>
  <div class="b">
    <div class="t">{_e(r.title)} {sample}</div>
    <div class="m">{_e(when)}{' &middot; ' + _e(model) if model else ''}</div>
    <div class="row"><span><b class="tnum">{_e(_dget(facts, "total_strokes", 0))}</b> strokes</span>
      <span><b class="tnum">{_e(n_clip)}</b> rallies</span>
      <span><b class="tnum">{_e(rt) if rt else "&mdash;"}&times;</b> realtime</span></div>
  </div>
</a>"""


# ---------------- session detail ----------------

def _timeline(sid: str, doc: dict, activity: dict | None) -> str:
    clips = [c for c in _dget(doc, "clips") or []
             if isinstance(c, dict) and isinstance(c.get("analysis"), dict)]
    duration = _num(_dget(doc, "video_duration_s")) or max(
        (_num(c["analysis"].get("end_s")) for c in clips), default=0) or 1.0
    W, H, pad, axis_h = 1080, 130, 14, 22
    inner = W - 2 * pad
    plot_h = H - axis_h

    def x(t: float) -> float:
        return pad + inner * max(0.0, min(1.0, t / duration))

    rank = {"low": 1, "medium": 2, "high": 3}
    s: list[str] = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="min-width:640px" role="img" aria-label="session timeline">']
    step = next((c for c in (15, 30, 60, 120, 300, 600, 900, 1800, 3600)
                 if duration / c <= 10), 3600)
    for i in range(int(duration // step) + 1):
        t = i * step
        s.append(f'<line x1="{x(t):.1f}" y1="{pad}" x2="{x(t):.1f}" y2="{plot_h}" stroke="var(--border)" stroke-width="1"/>')
        s.append(f'<text x="{x(t):.1f}" y="{H-6}" font-size="11" fill="var(--muted)" text-anchor="middle" style="font-variant-numeric:tabular-nums">{int(t//60)}:{int(t%60):02d}</text>')
    if isinstance(activity, dict) and activity.get("times") and activity.get("scores"):
        ts, sc = activity["times"], activity["scores"]
        mx = max(sc, default=0) or 1.0
        pts = " ".join(f"{x(t):.1f},{plot_h - 8 - (v/mx)*(plot_h-pad-24):.1f}" for t, v in zip(ts, sc))
        s.append(f'<polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="1" opacity="0.28"/>')
    for c in clips:
        a = c["analysis"]
        s0, s1 = _num(a.get("start_s")), _num(a.get("end_s"))
        s.append(f'<a href="#clip-{_e(c.get("index", 0))}"><rect x="{x(s0):.1f}" y="{plot_h-16}" '
                 f'width="{max(3, x(s1)-x(s0)):.1f}" height="11" rx="3" fill="var(--accent)" opacity="0.30"/></a>')
        strokes = a.get("strokes", [])
        xs = [x(_num(st.get("t_s")) or s0) for st in strokes]
        for j, st in enumerate(strokes):
            flags = (st.get("technique_flags") or []) + (st.get("tactical_flags") or [])
            worst = "none"
            for f in flags:
                sev = f.get("severity", "low")
                if worst == "none" or rank.get(sev, 0) > rank.get(worst, 0):
                    worst = sev
            color = SEV.get(worst, ("var(--ink-2)",))[0] if worst != "none" else "var(--ink-2)"
            tx = xs[j]
            gap = min((abs(tx - xs[k]) for k in range(len(xs)) if k != j), default=24.0)
            half = max(2.0, min(6.0, gap / 2 - 0.5))
            tip = f"<span class='h'>{_e(st.get('stroke', '?'))}</span> ({_e(st.get('player', '?'))}) &middot; t={_num(st.get('t_s')):.1f}s"
            out = st.get("outcome")
            if out in ("net", "out_long", "out_wide"):
                tip += f" &middot; {_e({'net': 'net', 'out_long': 'long', 'out_wide': 'wide'}[out])}"
            if flags:
                tip += "<br>" + "<br>".join(f"{_e(f.get('code', ''))} <span class='s'>({_e(f.get('severity', ''))})</span>" for f in flags[:4])
            s.append(f'<line x1="{tx:.1f}" y1="{pad+4}" x2="{tx:.1f}" y2="{plot_h-20}" stroke="{color}" stroke-width="2"/>')
            s.append(f'<rect x="{tx-half:.1f}" y="{pad}" width="{2*half:.1f}" height="{plot_h-pad-16}" fill="transparent" data-tip="{_e(tip)}"/>')
    s.append("</svg>")
    legend = (
        '<div class="legend">'
        f'<span><span class="sw" style="background:var(--accent);opacity:.4"></span>rally</span>'
        f'<span><span class="sw" style="background:{SEV["high"][0]}"></span>critical flag</span>'
        f'<span><span class="sw" style="background:{SEV["medium"][0]}"></span>warning flag</span>'
        f'<span><span class="sw" style="background:{SEV["low"][0]}"></span>minor / clean</span>'
        "<span class='muted'>hover a stroke for detail &middot; click a rally to jump</span></div>"
    )
    return f'<div class="card"><h2>Session timeline</h2><p class="hint">Every stroke Courtside found, on one axis.</p><div class="timeline-wrap">{"".join(s)}</div>{legend}</div>'


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
    model = str(_dget(doc, "model") or "")
    when = str(_dget(doc, "created_at") or "")
    badge = ('<span class="badge on"><span class="pulse"></span> Processed on-device &middot; network not used</span>'
             if _dget(doc, "on_device") else '<span class="badge">Server backend</span>')
    parts.append('<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0 14px">'
                 f'{badge}<span class="crumb">{_e(model)}</span><span class="crumb">{_e(when[:16].replace("T", " "))}</span>'
                 f'<span style="flex:1"></span>'
                 f'<a class="btn" href="/export/{_e(sid)}">{_ICONS["download"]} Export report</a></div>')

    if _dget(rs, "total_tokens") is not None:
        try:
            parts.append(f'<div class="banner">{_ICONS["bolt"]}<span>{_e(cost_summary_line(rs))}</span></div>')
        except (KeyError, TypeError, ValueError):
            pass

    fail = _dget(rs, "clips_failed") or 0
    rt = _dget(rs, "realtime_factor")
    peak = _dget(rs, "peak_gb")
    cloud = _dget(rs, "cloud_equiv_usd")
    part = _dget(facts, "partial") or {}
    partial = isinstance(part, dict) and _num(part.get("rallies_detected")) > _num(part.get("rallies_analyzed"))
    if partial:
        parts.append(f'<div class="banner err">{_ICONS["bolt"]}<span><b>Partial run:</b> only the first '
                     f'{int(_num(part.get("rallies_analyzed")))} of {int(_num(part.get("rallies_detected")))} '
                     f'detected rallies were analyzed (quick test). Re-run without '
                     f'&ldquo;quick test&rdquo; to cover the whole match.</span></div>')

    cov = _dget(facts, "coverage") or {}
    cov_sub = ""
    if partial:
        cov_sub = (f'<div class="sub" style="color:var(--crit)">first '
                   f'{int(_num(part.get("rallies_analyzed")))} of '
                   f'{int(_num(part.get("rallies_detected")))} rallies</div>')
    elif isinstance(cov, dict) and _num(cov.get("downtime_removed_min")) >= 1:
        cov_sub = (f'<div class="sub">{_num(cov.get("downtime_removed_min")):.0f} min '
                   f'dead time skipped</div>')
    oc = _dget(facts, "outcomes") or {}
    err_tile = ""
    if _dget(oc, "decided"):
        err_tile = (f'<div class="tile"><b class="tnum">{int(_num(_dget(oc, "errors")))}</b>'
                    f'<span>errors (AI-called)</span>'
                    f'<div class="sub">{int(_num(_dget(oc, "coverage_pct")))}% of strokes decided</div></div>')
    parts.append(f"""
<div class="tiles">
  <div class="tile"><b class="tnum">{_e(_dget(facts, "total_strokes", 0))}</b><span>strokes</span></div>
  <div class="tile"><b class="tnum">{_e(_dget(facts, "clips_analyzed", 0))}</b><span>rallies</span>{cov_sub}</div>
  {err_tile}
  <div class="tile"><b class="tnum">{_e(_dget(split, "near", 0))} / {_e(_dget(split, "far", 0))}</b><span>near / far</span></div>
  <div class="tile accent"><b class="tnum">{_e(rt) if rt else "&mdash;"}&times;</b><span>realtime</span><div class="sub">end-to-end</div></div>
  <div class="tile"><b class="tnum">${_e(cloud) if cloud is not None else "0.00"}</b><span>{"cloud-equiv" if _dget(doc, "on_device") else "cloud cost (est.)"}</span>{'<div class="sub">on-device: $0</div>' if _dget(doc, "on_device") else '<div class="sub">frames uploaded</div>'}</div>
  <div class="tile"><b class="tnum">{_e(peak) if peak else "&mdash;"}</b><span>peak GB</span>{f'<div class="sub">{_e(fail)} clip(s) skipped</div>' if fail else ''}</div>
</div>""")

    parts.append(_timeline(sid, doc, activity))

    cq = _dget(_dget(doc, "contact_quality") or {}, "summary") or {}
    if cq.get("strokes_measured"):
        parts.append(_contact_quality_panel(cq))

    sr = _dget(doc, "serve_return") or {}
    sr_counts = _dget(sr, "counts") or {}
    if isinstance(sr_counts, dict) and any(_num(v) for v in sr_counts.values()):
        parts.append(_serve_return_panel(sr))

    em = _dget(doc, "error_matrix") or {}
    if _dget(em, "court_detected") and _dget(em, "players"):
        parts.append(_error_matrix_panel(em))

    # manual court calibration: the guaranteed path to the zone map when
    # auto-detection fails on this footage - one time, ten seconds, no re-run
    court_ok = bool(_dget(em, "court_detected")
                    or (_dget(sr, "court_detected") and (_dget(sr, "positions") or [])))
    has_measured = bool(_dget(_dget(doc, "contact_quality") or {}, "strokes"))
    if not court_ok and has_measured and (sdir / "court_anchor.jpg").exists():
        parts.append(_calibration_card(sid))

    def img_url(fp: Path) -> str | None:
        try:
            rel = fp.relative_to(sdir)
        except ValueError:
            return None
        return f"/frames/{sid}/{rel.as_posix()}"

    # experimental court map: where the errors happened
    cm_path = sdir / "courtmap.json"
    if cm_path.exists():
        try:
            cm = json.loads(cm_path.read_text())
            if isinstance(cm, dict) and cm.get("positions"):
                parts.append(_court_panel(cm))
        except (OSError, json.JSONDecodeError):
            pass

    deep = [m for m in (_dget(doc, "moments") or []) if isinstance(m, dict)]
    if deep:
        parts.append('<div class="kicker">Coaching moments<span class="rule"></span></div>')
        for m in deep:
            parts.append(_moment_card(sid, m))
    else:
        moments = collect_flag_moments(sdir, doc, img_fn=img_url)
        if moments:
            parts.append('<div class="kicker">Flagged moments<span class="rule"></span></div>')
            cards = []
            for m in moments:
                img = (f'<img loading="lazy" src="{_e(m["img"])}" alt="flagged frame">' if m["img"]
                       else '<div style="aspect-ratio:16/9;background:#000"></div>')
                cards.append(f"""
<div class="mcard">{img}<div class="b">{_sev_chip(m["severity"])}
  <span class="code">{_e(m["code"])}</span>
  <div class="mm">{_e(m["kind"])} &middot; {_e(m["stroke"])} ({_e(m["player"])}) &middot; t={_num(m["t_s"]):.1f}s</div>
  <div class="ev">{_e(m["evidence"])}</div></div></div>""")
            parts.append(f'<div class="mgrid">{"".join(cards)}</div>')

    def _rally_card(c: dict) -> str:
        idx = _e(c.get("index", "?"))
        a = c["analysis"]
        frames = _clip_frames(sdir, c)
        thumbs = frames[:: max(1, len(frames) // 3)][:3]
        urls = [u for u in (img_url(t) for t in thumbs) if u]
        timg = "".join(f'<img loading="lazy" src="{_e(u)}" alt="frame">' for u in urls)
        n_flags = sum(len(s.get("technique_flags") or []) + len(s.get("tactical_flags") or [])
                      for s in a.get("strokes", []) if isinstance(s, dict))
        return f"""
<div class="card" id="clip-{idx}"><div class="rally">
  <div class="fr">{timg}</div>
  <div><div class="t">Clip {idx} <span class="m">{_num(a.get("start_s")):.1f}&ndash;{_num(a.get("end_s")):.1f}s</span></div>
    <div class="m">{len(a.get("strokes", []))} strokes &middot; {n_flags} flags &middot; confidence {_e(a.get("confidence", "?"))}</div>
    <p>{_e(a.get("rally_summary", ""))}</p></div>
</div></div>"""

    def _clip_noteworthy(c: dict) -> bool:
        a = c.get("analysis") or {}
        for s in a.get("strokes", []):
            if not isinstance(s, dict):
                continue
            if s.get("technique_flags") or s.get("tactical_flags"):
                return True
            if s.get("outcome") in ("net", "out_long", "out_wide"):
                return True
        return False

    clips_all = [c for c in (_dget(doc, "clips") or []) if isinstance(c, dict)]
    ok_clips = [c for c in clips_all if c.get("status") == "ok" and isinstance(c.get("analysis"), dict)]
    skipped = [c for c in clips_all if c not in ok_clips]
    with_strokes = [c for c in ok_clips if (c.get("analysis") or {}).get("strokes")]
    quiet = [c for c in ok_clips if c not in with_strokes]
    notable = [c for c in with_strokes if _clip_noteworthy(c)]
    routine = [c for c in with_strokes if c not in notable]

    parts.append('<div class="kicker">Rallies<span class="rule"></span></div>')
    if notable:
        parts.append(f'<p class="hint" style="margin:0 0 10px">{len(notable)} of {len(ok_clips)} '
                     f'rallies carry errors or flags &mdash; shown first.</p>')
        parts.extend(_rally_card(c) for c in notable)
    else:
        parts.extend(_rally_card(c) for c in with_strokes[:8])
        routine = with_strokes[8:]
    if routine:
        parts.append(f'<details class="card"><summary style="cursor:pointer;font-weight:650">'
                     f'Show {len(routine)} more rallies (no flags or error outcomes)</summary>'
                     + "".join(_rally_card(c) for c in routine) + "</details>")
    if quiet or skipped:
        lines = []
        for c in quiet:
            a = c.get("analysis") or {}
            lines.append(f'<div class="m" id="clip-{_e(c.get("index", "?"))}" style="padding:3px 0">'
                         f'Clip {_e(c.get("index", "?"))} &middot; '
                         f'{_num(a.get("start_s")):.1f}&ndash;{_num(a.get("end_s")):.1f}s &middot; '
                         f'no strokes ({_e(a.get("rally_summary", ""))})</div>')
        for c in skipped:
            lines.append(f'<div class="m" id="clip-{_e(c.get("index", "?"))}" style="padding:3px 0">'
                         f'Clip {_e(c.get("index", "?"))} &middot; skipped &mdash; '
                         f'{_e(c.get("error", "no analysis"))}</div>')
        parts.append(f'<details class="card"><summary style="cursor:pointer;font-weight:650">'
                     f'{len(quiet)} quiet clips (no strokes){" &middot; " + str(len(skipped)) + " skipped" if skipped else ""}'
                     f'</summary><div style="margin-top:8px">{"".join(lines)}</div></details>')
    # timeline anchors may point inside a closed <details>: open it on the way
    parts.append("""<script>
function openClipTarget(){var h=location.hash;if(!h)return;var el=document.querySelector(h.replace(/([.:])/g,'\\\\$1'));
if(!el)return;var d=el.closest('details');if(d)d.open=true;el.scrollIntoView({block:'center'});}
window.addEventListener('hashchange',openClipTarget);openClipTarget();
</script>""")

    md = sdir / "session_report.md"
    if md.exists():
        parts.append('<div class="kicker">Coaching report<span class="rule"></span></div>')
        parts.append(f'<div class="card rep">{_md_to_html(md.read_text())}</div>')

    return _page(ref.title, "".join(parts), crumb="session")


_CQ_COLORS = {"ideal": "var(--good)", "acceptable": "var(--warn)", "poor": "var(--crit)"}


def _contact_quality_panel(cq: dict) -> str:
    """Strike-zone success factor: ball height at contact, session-wide."""
    rows = []
    by_type = _dget(cq, "by_type") or {}
    for st, counts in by_type.items():
        if not isinstance(counts, dict):
            continue
        total = sum(_num(v) for v in counts.values()) or 1
        segs = "".join(
            f'<div data-tip="{q}: {int(_num(counts.get(q)))}" '
            f'style="width:{100 * _num(counts.get(q)) / total:.1f}%;background:{_CQ_COLORS[q]}"></div>'
            for q in ("ideal", "acceptable", "poor") if _num(counts.get(q)) > 0)
        rows.append(
            f'<div style="display:flex;align-items:center;gap:12px;margin:7px 0">'
            f'<span style="width:120px;font-size:12.5px;color:var(--ink-2)">{_e(st)}</span>'
            f'<div style="flex:1;display:flex;height:14px;border-radius:7px;overflow:hidden;'
            f'border:1px solid var(--border-2)">{segs}</div>'
            f'<span class="tnum" style="width:34px;text-align:right;font-size:12px;color:var(--muted)">'
            f'{int(total)}</span></div>')
    legend = "".join(f'<span><span class="sw" style="background:{c}"></span>{q}</span>'
                     for q, c in _CQ_COLORS.items())
    return f"""
<div class="card"><h2>Contact height &mdash; strike zone</h2>
  <p class="hint">Ball height at contact relative to the player's own body zones
  (2D image-plane; wrist proxy when the ball isn't detected). Groundstroke ideal: hip-to-chest.</p>
  <div style="display:flex;gap:22px;align-items:center;margin-bottom:10px">
    <span class="stat" style="margin:0"><b class="tnum" style="font-size:26px;display:block;
      color:var(--accent)">{_e(cq.get("pct_ideal", 0))}%</b>
      <span style="font-size:12px;color:var(--ink-2)">strokes in the ideal zone</span></span>
    <span class="crumb tnum">{_e(cq.get("strokes_measured", 0))} strokes measured</span>
  </div>
  {"".join(rows)}
  <div class="legend">{legend}</div>
</div>"""


_ZONE_BANDS = ((1, 0.0, 2.5), (2, 2.5, 6.40), (3, 6.40, 9.5),
               (4, 9.5, 12.385), (5, 12.385, 14.4))  # dist-from-net bands
_RUNWAY_X = {"C-L": (-1.1, 1.37), "B-L": (1.37, 4.113), "A": (4.113, 6.857),
             "B-R": (6.857, 9.6), "C-R": (9.6, 12.07)}  # near-player x ranges


def _court_heat_svg(items: list[dict], cells_by_player: dict | None = None,
                    width: int = 250) -> str:
    from .court import COURT_L, COURT_W
    """The court graphic done properly: an apron so behind-baseline positions
    have a home, a faint zone x runway grid, cells tinted by error density,
    and overlapping positions clustered into one counted marker.

    items: [{x_m, y_m, color, rank, halo, tip}] - rank picks the cluster color
    (higher = shown), halo draws the flagged ring, tips concatenate.
    """
    W, L = COURT_W, COURT_L
    AX, AY = 1.35, 2.7          # lateral / baseline apron in meters
    S, M = 12, 10
    vw = int((W + 2 * AX) * S + 2 * M)
    vh = int((L + 2 * AY) * S + 2 * M)

    def X(x: float) -> float: return M + (x + AX) * S
    def Y(y: float) -> float: return M + (y + AY) * S
    net_y = L / 2
    sngl = (W - 8.23) / 2
    svl = 6.40
    s = [f'<svg viewBox="0 0 {vw} {vh}" width="{width}" style="max-width:100%" role="img" aria-label="court map">']
    # apron: the legal standing room behind the lines
    s.append(f'<rect x="{M}" y="{M}" width="{vw - 2 * M}" height="{vh - 2 * M}" '
             f'fill="var(--accent-wash)" opacity="0.45" rx="6"/>')

    # heat cells under the lines
    if cells_by_player:
        peak = max((int(_num(c.get("errors"))) for p in cells_by_player.values()
                    if isinstance(p, dict) for c in (p.get("cells") or {}).values()
                    if isinstance(c, dict)), default=0)
        for player, pdoc in cells_by_player.items():
            if not isinstance(pdoc, dict):
                continue
            for key, c in (pdoc.get("cells") or {}).items():
                errs = int(_num(c.get("errors"))) if isinstance(c, dict) else 0
                if not errs or not peak:
                    continue
                zpart, runway = (str(key).split(":", 1) + [""])[:2]
                band = next((b for b in _ZONE_BANDS if f"z{b[0]}" == zpart), None)
                xr = _RUNWAY_X.get(runway)
                if not band or not xr:
                    continue
                d0, d1 = band[1], band[2]
                if player == "near":
                    y0, y1 = net_y + d0, min(net_y + d1, L + AY)
                    x0, x1 = xr
                else:
                    y0, y1 = max(net_y - d1, -AY), net_y - d0
                    x0, x1 = W - xr[1], W - xr[0]
                pct = int(14 + 60 * errs / peak)
                s.append(f'<rect x="{X(x0):.1f}" y="{Y(y0):.1f}" width="{(x1 - x0) * S:.1f}" '
                         f'height="{(y1 - y0) * S:.1f}" '
                         f'fill="color-mix(in srgb, var(--crit) {pct}%, transparent)"/>')

    # faint zone / runway grid so the dots tie back to the chart
    grid = 'stroke="var(--ink-2)" stroke-width="0.7" opacity="0.22" stroke-dasharray="3 4"'
    for _z, d0, _d1 in _ZONE_BANDS[1:]:
        for y in (net_y - d0, net_y + d0):
            s.append(f'<line x1="{X(-AX)}" y1="{Y(y):.1f}" x2="{X(W + AX)}" y2="{Y(y):.1f}" {grid}/>')
    for xb in (1.37, 4.113, 6.857, 9.6):
        s.append(f'<line x1="{X(xb):.1f}" y1="{Y(-AY)}" x2="{X(xb):.1f}" y2="{Y(L + AY)}" {grid}/>')

    # court proper
    s.append(f'<rect x="{X(0)}" y="{Y(0)}" width="{W * S}" height="{L * S}" fill="none" '
             f'stroke="var(--ink-2)" stroke-width="1.6" rx="2"/>')
    for x0, x1, y0, y1 in ((sngl, sngl, 0, L), (W - sngl, W - sngl, 0, L),
                           (sngl, W - sngl, net_y - svl, net_y - svl),
                           (sngl, W - sngl, net_y + svl, net_y + svl),
                           (W / 2, W / 2, net_y - svl, net_y + svl)):
        s.append(f'<line x1="{X(x0):.1f}" y1="{Y(y0):.1f}" x2="{X(x1):.1f}" y2="{Y(y1):.1f}" '
                 f'stroke="var(--ink-2)" stroke-width="1" opacity="0.6"/>')
    s.append(f'<line x1="{X(0) - 6}" y1="{Y(net_y):.1f}" x2="{X(W) + 6}" y2="{Y(net_y):.1f}" '
             f'stroke="var(--ink)" stroke-width="2.5"/>')

    # cluster overlapping positions into one counted marker
    clusters: dict[tuple[int, int], dict] = {}
    for it in items:
        if not isinstance(it.get("x_m"), (int, float)) or not isinstance(it.get("y_m"), (int, float)):
            continue
        key = (round(it["x_m"] / 0.5), round(it["y_m"] / 0.5))
        c = clusters.setdefault(key, {"x": 0.0, "y": 0.0, "n": 0, "rank": -1,
                                      "color": "var(--warn)", "halo": False, "tips": []})
        c["x"] += it["x_m"]
        c["y"] += it["y_m"]
        c["n"] += 1
        c["halo"] = c["halo"] or bool(it.get("halo"))
        if it.get("rank", 0) > c["rank"]:
            c["rank"], c["color"] = it.get("rank", 0), it.get("color", "var(--warn)")
        if it.get("tip"):
            c["tips"].append(it["tip"])
    marks = []
    for c in clusters.values():
        r = 6.0 if c["n"] == 1 else min(11.0, 7.0 + c["n"])
        marks.append({"cx": X(c["x"] / c["n"]), "cy": Y(c["y"] / c["n"]), "r": r, **c})
    # relax collisions: nudge overlapping cluster markers apart so counts and
    # colors stay readable when play concentrates in one spot
    for _pass in range(3):
        for i in range(len(marks)):
            for j in range(i + 1, len(marks)):
                a, b = marks[i], marks[j]
                dx, dy = b["cx"] - a["cx"], b["cy"] - a["cy"]
                dist = max(0.001, (dx * dx + dy * dy) ** 0.5)
                need = a["r"] + b["r"] + 2 - dist
                if need > 0:
                    ux, uy = dx / dist, dy / dist
                    a["cx"] -= ux * need / 2
                    a["cy"] -= uy * need / 2
                    b["cx"] += ux * need / 2
                    b["cy"] += uy * need / 2
    # draw least-severe first so the worst markers stay on top
    labels = []
    for c in sorted(marks, key=lambda m: m["rank"]):
        cx, cy, r = c["cx"], c["cy"], c["r"]
        tip = "<br>".join(c["tips"][:4]) + (f"<br>&hellip; +{len(c['tips']) - 4} more"
                                            if len(c["tips"]) > 4 else "")
        if c["halo"]:
            s.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r + 4}" fill="none" '
                     f'stroke="var(--crit)" stroke-width="1.4" opacity="0.8"/>')
        s.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{c["color"]}" '
                 f'stroke="var(--surface)" stroke-width="2" data-tip="{_e(tip)}"/>')
        if c["n"] > 1:
            labels.append(f'<text x="{cx:.1f}" y="{cy + 3.5:.1f}" text-anchor="middle" '
                          f'font-size="9.5" font-weight="700" fill="#fff" '
                          f'style="pointer-events:none">{c["n"]}</text>')
    s.extend(labels)
    s.append("</svg>")
    return "".join(s)


def _court_svg_base(W: float, L: float, S: int, M: int) -> tuple[list[str], int, int]:
    """Shared portrait court drawing (outline, singles lines, service boxes, net)."""
    vw, vh = int(W * S + 2 * M), int(L * S + 2 * M)
    def X(x: float) -> float: return M + x * S
    def Y(y: float) -> float: return M + y * S
    sngl = (W - 8.23) / 2
    svl = 6.40  # service line is 6.40m from the net
    net_y = L / 2
    s = [f'<svg viewBox="0 0 {vw} {vh}" width="250" style="max-width:100%" role="img" aria-label="court">']
    s.append(f'<rect x="{X(0)}" y="{Y(0)}" width="{W*S}" height="{L*S}" fill="var(--accent-wash)" '
             f'stroke="var(--ink-2)" stroke-width="1.5" rx="2"/>')
    for x0, x1, y0, y1 in ((sngl, sngl, 0, L), (W - sngl, W - sngl, 0, L),
                           (sngl, W - sngl, net_y - svl, net_y - svl),
                           (sngl, W - sngl, net_y + svl, net_y + svl),
                           (W / 2, W / 2, net_y - svl, net_y + svl)):
        s.append(f'<line x1="{X(x0)}" y1="{Y(y0)}" x2="{X(x1)}" y2="{Y(y1)}" '
                 f'stroke="var(--ink-2)" stroke-width="1" opacity="0.6"/>')
    s.append(f'<line x1="{X(0)-6}" y1="{Y(net_y)}" x2="{X(W)+6}" y2="{Y(net_y)}" '
             f'stroke="var(--ink)" stroke-width="2.5"/>')
    return s, S, M


def _serve_return_panel(sr: dict) -> str:
    """Serves & returns: court position heatmap + return-height zone distribution."""
    counts = _dget(sr, "counts") or {}
    positions = [p for p in (_dget(sr, "positions") or []) if isinstance(p, dict)]
    rh = _dget(sr, "return_height") or {}

    court = ""
    if positions:
        qrank = {"ideal": 1, "acceptable": 2, "poor": 3}
        items = []
        for p in positions:
            q = str(p.get("quality", "acceptable"))
            items.append({
                "x_m": p.get("x_m"), "y_m": p.get("y_m"),
                "color": _CQ_COLORS.get(q, "var(--warn)"),
                "rank": qrank.get(q, 2) + (3 if p.get("flagged") else 0),
                "halo": bool(p.get("flagged")),
                "tip": (f"{p.get('stroke', '')} ({p.get('player', '')}) &middot; "
                        f"{str(p.get('zone', '')).replace('_', ' ')} &middot; t={_num(p.get('t_s')):.1f}s"
                        + (" &middot; flagged" if p.get("flagged") else "")),
            })
        court = f'<div style="flex:0 0 auto">{_court_heat_svg(items)}</div>'

    # zone table: worst zones first (by poor+flagged density)
    zsum = _dget(sr, "zones_summary") or {}
    rows: list[str] = []
    def badness(v: dict) -> float:
        c = max(1, int(_num(v.get("count"))))
        return (_num(v.get("poor")) + _num(v.get("flagged"))) / c
    zitems = [(k, v) for k, v in zsum.items() if isinstance(v, dict)]
    for key, v in sorted(zitems, key=lambda kv: -badness(kv[1]))[:8]:
        stroke, zone = (key.split(":", 1) + [""])[:2]
        c, poor, fl = int(_num(v.get("count"))), int(_num(v.get("poor"))), int(_num(v.get("flagged")))
        warn = ' style="color:var(--crit)"' if (poor + fl) > 0 else ""
        rows.append(f'<div style="display:flex;gap:10px;font-size:12.5px;padding:4px 0;'
                    f'border-bottom:1px solid var(--border-2)">'
                    f'<span style="width:56px;color:var(--ink-2)">{_e(stroke)}</span>'
                    f'<span style="flex:1">{_e(zone.replace("_", " "))}</span>'
                    f'<span class="tnum">{c}&times;</span>'
                    f'<span class="tnum"{warn}>{poor} poor &middot; {fl} flagged</span></div>')

    # return-height distribution
    zones_order = ["below_knee", "knee_to_hip", "hip_to_chest", "chest_to_shoulder",
                   "shoulder_to_head", "above_head"]
    hz = _dget(rh, "zones") or {}
    total_h = sum(_num(v) for v in hz.values()) or 0
    hbars = ""
    if total_h:
        segs = []
        for z in zones_order:
            n = int(_num(hz.get(z)))
            if n:
                q = "ideal" if z == "hip_to_chest" else ("poor" if z in ("below_knee", "above_head") else "acceptable")
                segs.append(f'<div data-tip="{_e(z.replace("_", " "))}: {n}" '
                            f'style="width:{100 * n / total_h:.1f}%;background:{_CQ_COLORS[q]}"></div>')
        hq = _dget(rh, "quality") or {}
        hbars = (f'<div class="kicker" style="margin:14px 0 8px">Return-of-serve contact height</div>'
                 f'<div style="display:flex;height:14px;border-radius:7px;overflow:hidden;'
                 f'border:1px solid var(--border-2)">{"".join(segs)}</div>'
                 f'<div class="legend" style="margin-top:6px">'
                 f'<span>{int(_num(hq.get("ideal")))} ideal</span>'
                 f'<span>{int(_num(hq.get("acceptable")))} acceptable</span>'
                 f'<span style="color:var(--crit)">{int(_num(hq.get("poor")))} poor</span></div>')

    note = ("Player position at contact (fixed-camera homography). Serve landing placement "
            "is not claimed - that needs ball tracking (production roadmap).")
    legend = ('<div class="legend" style="margin-top:8px">'
              '<span><span class="sw" style="background:var(--good)"></span>serve (dot) / return (ring), colored by contact quality</span>'
              '<span><span class="sw" style="border:1.5px solid var(--crit);background:transparent"></span>flagged stroke</span></div>')
    body = (f'<div style="display:flex;gap:22px;flex-wrap:wrap">{court}'
            f'<div style="flex:1;min-width:260px">'
            f'<div style="display:flex;gap:18px;margin-bottom:8px">'
            f'<span class="stat" style="margin:0"><b class="tnum" style="font-size:24px;display:block">'
            f'{int(_num(_dget(counts, "serves")))}</b><span style="font-size:12px;color:var(--ink-2)">serves</span></span>'
            f'<span class="stat" style="margin:0"><b class="tnum" style="font-size:24px;display:block">'
            f'{int(_num(_dget(counts, "returns")))}</b><span style="font-size:12px;color:var(--ink-2)">returns</span></span></div>'
            + ("".join(rows) if rows else
               ('' if positions else '<p class="hint">Court not detected in this footage - showing height zones only.</p>'))
            + hbars + "</div></div>")
    return (f'<div class="card"><h2>Serves &amp; returns &mdash; where and how high</h2>'
            f'<p class="hint">{note}</p>{body}{legend if positions else ""}</div>')


_RUNWAY_COLS = ("C-L", "B-L", "A", "B-R", "C-R")
_ZONE_ROWS = ((1, "net"), (2, "mid-court"), (3, "no-man's"), (4, "baseline"), (5, "back"))


def _error_matrix_panel(em: dict) -> str:
    """Zone (1-5) x runway (C-L..C-R) error grid - the manual error-tracking
    template coaches already know, filled in automatically. Per player."""
    players = _dget(em, "players") or {}
    grids: list[str] = []
    for player in ("near", "far"):
        pdoc = players.get(player)
        if not isinstance(pdoc, dict) or not pdoc.get("measured"):
            continue
        cells = pdoc.get("cells") or {}
        max_err = max((int(_num(c.get("errors"))) for c in cells.values()
                       if isinstance(c, dict)), default=0)
        rows = ['<div style="display:grid;grid-template-columns:96px repeat(5,minmax(44px,1fr));'
                'gap:3px;font-size:11.5px">']
        rows.append('<div></div>' + "".join(
            f'<div style="text-align:center;color:var(--muted);padding:2px 0">{_e(r)}</div>'
            for r in _RUNWAY_COLS))
        for znum, zname in _ZONE_ROWS:
            rows.append(f'<div style="color:var(--ink-2);align-self:center;text-align:right;'
                        f'padding-right:8px"><b class="tnum">{znum}</b> &middot; {_e(zname)}</div>')
            for runway in _RUNWAY_COLS:
                c = cells.get(f"z{znum}:{runway}")
                if not isinstance(c, dict) or not c.get("measured"):
                    rows.append('<div style="border:1px solid var(--border-2);border-radius:6px;'
                                'aspect-ratio:1.5;display:flex;align-items:center;justify-content:center;'
                                'color:var(--muted)">&middot;</div>')
                    continue
                errs, meas = int(_num(c.get("errors"))), int(_num(c.get("measured")))
                if errs and max_err:
                    pct = int(12 + 68 * errs / max_err)
                    bg = f"color-mix(in srgb, var(--crit) {pct}%, transparent)"
                else:
                    bg = "var(--accent-wash)"
                bits = [f"{c.get(k, 0)} {label}" for k, label in
                        (("net", "net"), ("out_long", "long"), ("out_wide", "wide"),
                         ("flagged", "flagged"), ("poor_contact", "poor contact"))
                        if int(_num(c.get(k)))]
                tip = f"<span class='h'>zone {znum} ({zname}) &middot; runway {runway}</span>" \
                      + (f"<br>{' &middot; '.join(bits)}" if bits else "<br>no errors")
                rows.append(f'<div data-tip="{_e(tip)}" style="border:1px solid var(--border-2);'
                            f'border-radius:6px;aspect-ratio:1.5;display:flex;flex-direction:column;'
                            f'align-items:center;justify-content:center;background:{bg}">'
                            f'<b class="tnum">{errs}</b>'
                            f'<span style="font-size:10px;color:var(--ink-2)">/{meas}</span></div>')
        rows.append("</div>")
        grids.append(f'<div style="flex:1;min-width:300px"><div class="kicker" style="margin:0 0 6px">'
                     f'{_e(player)} player &mdash; {int(_num(pdoc.get("errors")))} errors / '
                     f'{int(_num(pdoc.get("measured")))} measured</div>{"".join(rows)}</div>')

    # ranked worst cells
    rows_w: list[str] = []
    for w in (_dget(em, "worst_cells") or [])[:5]:
        if not isinstance(w, dict):
            continue
        zpart, runway = (str(w.get("cell", "")).split(":", 1) + [""])[:2]
        znum = zpart.lstrip("z")
        zname = dict(_ZONE_ROWS).get(int(znum)) if znum.isdigit() else None
        cell_label = (f"zone {znum} ({zname}) &middot; {runway}" if zname
                      else str(w.get("cell", "")).replace("_", " "))
        rows_w.append(f'<div style="display:flex;gap:10px;font-size:12.5px;padding:4px 0;'
                      f'border-bottom:1px solid var(--border-2)">'
                      f'<span style="width:44px;color:var(--ink-2)">{_e(w.get("player", ""))}</span>'
                      f'<span style="flex:1">{cell_label}</span>'
                      f'<span class="tnum" style="color:var(--crit)">{int(_num(w.get("errors")))} errors</span>'
                      f'<span class="tnum">of {int(_num(w.get("measured")))}</span></div>')
    worst = (f'<div style="flex:0 1 280px;min-width:240px"><div class="kicker" style="margin:0 0 6px">'
             f'Worst cells</div>{"".join(rows_w)}</div>') if rows_w else ""

    # court heat layer: cells tinted by error density, error positions clustered
    dots = ""
    positions = [p for p in (_dget(em, "positions") or []) if isinstance(p, dict)]
    if positions or players:
        items = []
        for p in positions:
            causes = [c for c in (p.get("causes") or []) if isinstance(c, str)]
            out = any(c in ("net", "out_long", "out_wide") for c in causes)
            color = ("var(--crit)" if out
                     else ("var(--warn)" if "flag" in causes else "var(--accent)"))
            items.append({
                "x_m": p.get("x_m"), "y_m": p.get("y_m"), "color": color,
                "rank": 3 if out else (2 if "flag" in causes else 1), "halo": False,
                "tip": (f"{p.get('stroke', '')} ({p.get('player', '')}) &middot; "
                        f"t={_num(p.get('t_s')):.1f}s &middot; "
                        f"{', '.join(c.replace('_', ' ') for c in causes)}"),
            })
        legend = ('<div class="legend" style="margin-top:6px">'
                  '<span><span class="sw" style="background:var(--crit)"></span>ball out (AI-called)</span>'
                  '<span><span class="sw" style="background:var(--warn)"></span>flagged form</span>'
                  '<span><span class="sw" style="background:var(--accent)"></span>poor contact</span>'
                  '<span class="muted">shaded cells = error density</span></div>')
        dots = (f'<div style="flex:0 0 auto">'
                f'{_court_heat_svg(items, cells_by_player=players)}{legend}</div>')

    note = ("The zone (1=net &hellip; 5=back) &times; runway (C-L / B-L / A / B-R / C-R) "
            "error grid coaches track by hand, filled in automatically from video. "
            "Cells show where the player stood when errors happened (fixed-camera "
            "homography). Error = AI-called outcome (net/long/wide), a medium+ flag, or "
            "poor measured contact. Far-half positions have lower runway precision.")
    return (f'<div class="card"><h2>Error map &mdash; zone &times; runway</h2>'
            f'<p class="hint">{note}</p>'
            f'<div style="display:flex;gap:26px;flex-wrap:wrap">{dots}{"".join(grids)}{worst}</div></div>')


_CAL_ORDER = ("far-left corner", "far-right corner", "near-right corner", "near-left corner")


def _calibration_card(sid: str) -> str:
    """Click the 4 doubles-court corners once -> zone map + error grid rebuild
    instantly from already-measured data (no re-analysis, no model calls)."""
    steps = " &rarr; ".join(_CAL_ORDER)
    return f"""
<div class="card" id="calibrate">
  <h2>Calibrate the court &mdash; unlock the zone map</h2>
  <p class="hint">The court lines couldn't be auto-detected in this footage. Click the four corners of
     the <b>doubles court</b> in this order: <b>{steps}</b>. One time per camera setup; the error
     chart and serve/return map rebuild instantly from data already measured.</p>
  <div style="position:relative;display:inline-block;max-width:100%">
    <img id="calimg" src="/frames/{_e(sid)}/court_anchor.jpg"
         style="max-width:100%;display:block;border-radius:8px;cursor:crosshair">
    <svg id="calovl" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none"></svg>
  </div>
  <div style="display:flex;gap:10px;align-items:center;margin-top:10px;flex-wrap:wrap">
    <span class="kicker" id="calstep">Click: {_e(_CAL_ORDER[0])}</span>
    <span style="flex:1"></span>
    <button class="btn" type="button" onclick="calReset()">Reset</button>
    <button class="btn primary" type="button" id="calsave" onclick="calSave()" disabled>Save calibration</button>
  </div>
  <p class="note" id="calmsg" style="margin-top:6px"></p>
</div>
<script>
var calPts=[];
var calOrder={json.dumps(list(_CAL_ORDER))};
var calImg=document.getElementById('calimg');
function calDraw(){{
  var ovl=document.getElementById('calovl');
  var r=calImg.getBoundingClientRect();
  var s='';
  for(var i=0;i<calPts.length;i++){{
    var x=calPts[i][0]/calImg.naturalWidth*r.width, y=calPts[i][1]/calImg.naturalHeight*r.height;
    s+='<circle cx="'+x+'" cy="'+y+'" r="6" fill="#e0483e" stroke="#fff" stroke-width="2"/>';
  }}
  if(calPts.length>1){{
    var p=calPts.map(function(c){{return (c[0]/calImg.naturalWidth*r.width)+','+(c[1]/calImg.naturalHeight*r.height);}}).join(' ');
    s+='<polyline points="'+p+(calPts.length===4?' '+p.split(' ')[0]:'')+'" fill="none" stroke="#e0483e" stroke-width="2" stroke-dasharray="6 4"/>';
  }}
  ovl.innerHTML=s;
  document.getElementById('calstep').textContent=calPts.length<4?('Click: '+calOrder[calPts.length]):'All 4 corners set';
  document.getElementById('calsave').disabled=calPts.length!==4;
}}
calImg.addEventListener('click',function(e){{
  if(calPts.length>=4)return;
  var r=calImg.getBoundingClientRect();
  calPts.push([(e.clientX-r.left)/r.width*calImg.naturalWidth,(e.clientY-r.top)/r.height*calImg.naturalHeight]);
  calDraw();
}});
window.addEventListener('resize',calDraw);
function calReset(){{calPts=[];document.getElementById('calmsg').textContent='';calDraw();}}
function calSave(){{
  document.getElementById('calsave').disabled=true;
  fetch('/calibrate/'+{json.dumps(sid)},{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{corners:calPts}})}})
  .then(function(r){{return r.json();}})
  .then(function(d){{
    if(d.ok&&d.court_detected){{location.reload();}}
    else{{
      document.getElementById('calmsg').textContent=(d.error||d.note||'Calibration produced implausible positions')+
        ' - check the corner order (far-left first) and try again.';
      document.getElementById('calsave').disabled=false;
    }}
  }})
  .catch(function(){{document.getElementById('calmsg').textContent='Request failed - is the app still running?';
    document.getElementById('calsave').disabled=false;}});
}}
</script>"""


def _court_panel(cm: dict) -> str:
    """SVG court diagram with error-position dots (experimental heatmap)."""
    W, L = _num(cm.get("court_w_m")) or 10.97, _num(cm.get("court_l_m")) or 23.77
    # render portrait: x across (width), y down (length); 12px/m with margin
    S, M = 12, 30
    vw, vh = int(W * S + 2 * M), int(L * S + 2 * M)

    def X(x): return M + x * S
    def Y(y): return M + y * S

    sngl = (W - 8.23) / 2  # singles sideline inset
    svl = 6.40             # service line distance from net
    net_y = L / 2
    s = [f'<svg viewBox="0 0 {vw} {vh}" width="240" style="max-width:100%" role="img" aria-label="court map">']
    s.append(f'<rect x="{X(0)}" y="{Y(0)}" width="{W*S}" height="{L*S}" fill="var(--accent-wash)" '
             f'stroke="var(--ink-2)" stroke-width="1.5" rx="2"/>')
    for x0, x1, y0, y1 in ((sngl, sngl, 0, L), (W - sngl, W - sngl, 0, L),
                           (sngl, W - sngl, net_y - svl, net_y - svl),
                           (sngl, W - sngl, net_y + svl, net_y + svl),
                           (W / 2, W / 2, net_y - svl, net_y + svl)):
        s.append(f'<line x1="{X(x0)}" y1="{Y(y0)}" x2="{X(x1)}" y2="{Y(y1)}" '
                 f'stroke="var(--ink-2)" stroke-width="1" opacity="0.6"/>')
    s.append(f'<line x1="{X(0)-6}" y1="{Y(net_y)}" x2="{X(W)+6}" y2="{Y(net_y)}" '
             f'stroke="var(--ink)" stroke-width="2.5"/>')
    for p in cm.get("positions", []):
        if not isinstance(p, dict):
            continue
        if not isinstance(p.get("x_m"), (int, float)) or not isinstance(p.get("y_m"), (int, float)):
            continue
        color = SEV.get(str(p.get("severity", "low")), SEV["low"])[0]
        tip = f"<span class='h'>{_e(p.get('code', ''))}</span> ({_e(p.get('player', ''))}) &middot; t={_num(p.get('t_s')):.1f}s"
        s.append(f'<circle cx="{X(_num(p.get("x_m")))}" cy="{Y(_num(p.get("y_m")))}" r="7" '
                 f'fill="{color}" stroke="var(--surface)" stroke-width="2" data-tip="{_e(tip)}"/>')
    s.append("</svg>")
    return (f'<div class="card"><h2>Where the errors happened</h2>'
            f'<p class="hint">Experimental: fixed-camera court mapping of flagged-moment positions. Hover a dot.</p>'
            f'<div style="display:flex;justify-content:center">{"".join(s)}</div></div>')


_ANGLE_LABELS = {
    "elbow_right_deg": ("R elbow", "&deg;"), "elbow_left_deg": ("L elbow", "&deg;"),
    "knee_right_deg": ("R knee", "&deg;"), "knee_left_deg": ("L knee", "&deg;"),
    "hip_shoulder_separation_deg": ("hip-shoulder", "&deg;"),
    "stance_width_ratio": ("stance width", "&times; shoulders"),
    "contact_height_ratio": ("contact height", " (0=hip 1=shoulder)"),
}


def _moment_card(sid: str, m: dict) -> str:
    """One deep-dive coaching moment: media strip + biomechanics + VLM card."""
    assets = _dget(m, "assets") or {}
    poster = (f'/frames/{_e(sid)}/{_e(_dget(assets, "overlay"))}'
              if _dget(assets, "overlay") else "")
    poster_attr = f' poster="{poster}"' if poster else ""
    media = []
    for key, label, kind in (("overlay_video", "Biomechanics", "video"),
                             ("ghost_video", "vs your best", "video"),
                             ("slowmo", "Slow motion", "video"),
                             ("overlay", "Contact frame", "img")):
        rel = _dget(assets, key)
        if not rel:
            continue
        url = f"/frames/{_e(sid)}/{_e(rel)}"
        if kind == "video":
            pa = poster_attr if key != "slowmo" else ""
            media.append(f'<div><span class="medialbl">{label}</span>'
                         f'<video src="{url}"{pa} autoplay loop muted playsinline></video></div>')
        else:
            media.append(f'<div><span class="medialbl">{label}</span>'
                         f'<img loading="lazy" src="{url}" alt="{label}"></div>')
        if len(media) >= 3:
            break
    media_html = f'<div class="media">{"".join(media)}</div>' if media else ""

    angles = _dget(m, "angles") or {}
    chips = []
    ch = _dget(m, "contact_height") or {}
    if _dget(ch, "zone"):
        q = str(_dget(ch, "quality", "acceptable"))
        qc = _CQ_COLORS.get(q, "var(--warn)")
        proxy = " (wrist proxy)" if _dget(ch, "method") == "wrist_proxy" else ""
        chips.append(f'<span class="angle" style="border-color:{qc}">ball at contact '
                     f'<b>{_e(str(_dget(ch, "zone", "")).replace("_", " "))}</b> &middot; '
                     f'<b style="color:{qc}">{_e(q)}</b>{proxy}</span>')
    for key, (label, unit) in _ANGLE_LABELS.items():
        v = angles.get(key) if isinstance(angles, dict) else None
        if v is not None:
            chips.append(f'<span class="angle">{label} <b>{_e(v)}</b>{unit}</span>')
    angle_html = (f'<div class="anglerow">{"".join(chips)}'
                  f'<span class="angle" style="color:var(--muted)">2D image-plane</span></div>'
                  if chips else "")

    t = _num(m.get("contact_t_s") or m.get("t_s"))
    hd = (f'<div class="hd">{_sev_chip(str(m.get("severity", "low")))}'
          f'<span class="code">{_e(m.get("code", ""))}</span>'
          f'<span class="mm">{_e(m.get("kind", ""))} &middot; {_e(m.get("stroke", ""))} '
          f'({_e(m.get("player", ""))}) &middot; t={t:.1f}s</span></div>')

    card = _dget(m, "card") or {}
    body = ""
    if card:
        drill = _dget(card, "drill") or {}
        body = f"""
<div class="cgrid">
  <div class="cbox"><div class="k">What happened</div><p>{_e(_dget(card, "what_happened", ""))}</p></div>
  <div class="cbox"><div class="k">Why it matters</div><p>{_e(_dget(card, "why_it_matters", ""))}</p></div>
  <div class="cbox"><div class="k">The correction</div><p>{_e(_dget(card, "correction", ""))}</p></div>
</div>
<div class="target"><b>Target:</b> {_e(_dget(card, "target", ""))}</div>
<div class="drill"><span class="nm">Drill: {_e(_dget(drill, "name", ""))}</span>
  <span class="m">&middot; {_e(_dget(drill, "setup", ""))}</span>
  <div class="m" style="margin-top:3px">Success: {_e(_dget(drill, "success_criterion", ""))}</div></div>"""
    else:
        body = f'<p style="margin:0;color:var(--ink-2);font-size:13px">{_e(m.get("evidence", ""))}</p>'

    return f'<div class="dcard">{media_html}<div class="body">{hd}{angle_html}{body}</div></div>'


# ---------------- run / progress page ----------------

_PHASE_STEPS = [("download", "Download"), ("segment", "Segment"),
                ("extract", "Extract"), ("analyze", "Analyze"), ("report", "Report")]
_PHASE_ORDER = {"queued": 0, "downloading": 0, "segmenting": 1, "extracting": 2,
                "analyzing": 3, "reporting": 4, "done": 5, "failed": -1, "cancelled": -1}


def render_run_page(run) -> str:
    steps = "".join(
        f'<div class="phase" data-step="{i}"><div class="dot"></div>{label}</div>'
        for i, (_key, label) in enumerate(_PHASE_STEPS))
    body = f"""
<div class="card">
  <div class="status-row" id="strow">
    <span class="spin" id="spin"></span>
    <span class="big" id="stword">starting</span>
    <span class="crumb tnum" id="elapsed"></span>
    <span style="flex:1"></span>
    <span class="badge">{_e(run.video)}</span>
    <button class="btn btn-danger" id="cancelbtn" onclick="cancelRun()">{_ICONS['stop']} Cancel</button>
  </div>
  <div class="prog">
    <div class="progbar" id="pbar"><div class="fill" id="pfill"></div></div>
    <div class="phases" id="phases">{steps}</div>
  </div>
  <div class="console-h"><span class="lbl">Live log</span><span class="crumb" id="pctlabel"></span></div>
  <div class="console" id="log">starting&hellip;</div>
  <p class="hint" id="donebar" style="display:none;margin:14px 0 0">
    <a class="btn btn-pri" id="openlink" href="/">Open session</a></p>
</div>
<script>
var rid={json.dumps(run.rid)};
var PHASE_ORDER={json.dumps(_PHASE_ORDER)};
var fails=0;
function esc(s){{var d=document.createElement('div');d.textContent=s;return d.innerHTML;}}
function cancelRun(){{
  fetch('/cancel/'+rid,{{method:'POST'}}).catch(function(){{}});
  document.getElementById('cancelbtn').disabled=true;
  document.getElementById('cancelbtn').textContent='cancelling...';
}}
function setPhases(phase){{
  var idx=PHASE_ORDER[phase];if(idx===undefined)idx=0;
  document.querySelectorAll('.phase').forEach(function(el,i){{
    el.classList.remove('active','done');
    if(idx<0)return;
    if(i<idx)el.classList.add('done');else if(i===idx)el.classList.add('active');
  }});
}}
function poll(){{
  fetch('/api/runs/'+rid).then(function(r){{if(!r.ok)throw 0;return r.json();}}).then(function(d){{
    if(d.error||!Array.isArray(d.log))throw 0;
    fails=0;
    var log=document.getElementById('log');log.innerHTML=d.log.map(esc).join('\\n');log.scrollTop=log.scrollHeight;
    document.getElementById('elapsed').textContent=(d.elapsed_s||0).toFixed(0)+'s';
    var pct=d.pct||0;document.getElementById('pfill').style.width=Math.max(4,pct)+'%';
    document.getElementById('pctlabel').textContent=(d.phase||'')+' \\u00b7 '+pct+'%';
    setPhases(d.phase);
    if(d.status==='running'){{document.getElementById('stword').textContent=d.phase||'running';setTimeout(poll,1000);return;}}
    document.getElementById('spin').style.display='none';
    document.getElementById('cancelbtn').style.display='none';
    var w=document.getElementById('stword'),bar=document.getElementById('pbar'),done=document.getElementById('donebar'),a=document.getElementById('openlink');
    done.style.display='block';
    if(d.status==='done'&&d.session){{
      w.textContent='Analysis complete';a.href='/session/'+d.session;
      setTimeout(function(){{location.href=a.href;}},1400);
    }}else if(d.status==='done'){{
      w.textContent='Complete';a.textContent='Back to dashboard';
    }}else{{
      w.textContent=d.status==='cancelled'?'Cancelled':'Failed';w.style.color='var(--crit)';
      bar.classList.add('failed');a.textContent='Back to dashboard';
    }}
  }}).catch(function(){{
    if(++fails>=5){{
      document.getElementById('spin').style.display='none';
      var w=document.getElementById('stword');w.textContent='Connection lost';w.style.color='var(--crit)';
      document.getElementById('donebar').style.display='block';
      document.getElementById('openlink').textContent='Back to dashboard';return;
    }}
    setTimeout(poll,2000);
  }});
}}
poll();
</script>"""
    return _page("Analyzing", body, crumb=run.video)
