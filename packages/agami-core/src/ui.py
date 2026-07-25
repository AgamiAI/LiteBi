"""Shared HTML shell for the server's web pages (login, consent, admin).

One styling system + two shells — `auth_page` (centered sign-in/consent) and `admin_shell` (the admin
console with tab nav) — so every server-rendered page matches the agami product design (the same look
as the web app: brand blue #0b57d0, white surface, pill buttons, the agami logo). This carries into
the hosted/enterprise products, so it's built to look professional. Everything interpolated MUST go
through `esc()` — these pages render attacker-influenceable values (usernames, emails, query params).
Pure strings; no template engine; a tiny CSS-only drawer + a native `<details>` account menu (no JS).
"""

from __future__ import annotations

import html
import re

# Palette + components mirror the agami web app (brand #0b57d0, line #D2DBF1, chip #f4f5fb). Embedded
# so pages are self-contained — no build step, no asset pipeline. Layout is responsive: the media
# query at the end collapses the admin chrome and tightens the auth padding on a phone.
_CSS = """
:root{
  --brand:#0b57d0; --brand-600:#0a4ab1; --line:#d2dbf1; --chip:#f4f5fb;
  --ink:#171717; --muted:#737373; --bg:#ffffff; --ok:#047857; --ok-bg:#ecfdf5;
  --off:#737373; --off-bg:#f4f5fb; --danger:#b42318; --danger-bg:#fef3f2; --danger-line:#fecdca;
}
*{box-sizing:border-box}
body{
  margin:0; min-height:100vh; background:var(--bg); color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--brand);text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:20px;font-weight:600;letter-spacing:-.01em;margin:0}
.sub{color:var(--muted);font-size:14px;margin:4px 0 0}
.muted{color:var(--muted)}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);
  border:1px solid var(--line);border-radius:8px;padding:3px 8px;font-size:13px;color:var(--ink)}

/* form controls */
label{display:block;font-weight:550;font-size:13px;margin:16px 0 6px}
input[type=text],input[type=email],input[type=password],select{
  width:100%; height:46px; padding:0 14px; border:1px solid var(--line); border-radius:10px;
  background:#fff; font-size:15px; color:var(--ink); transition:border-color .15s, box-shadow .15s;
}
input:focus,select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px rgba(11,87,208,.14)}

/* buttons — pill, like the app */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:10px;width:100%;height:48px;
  padding:0 18px;border-radius:999px;border:1px solid var(--brand);background:var(--brand);color:#fff;
  font-size:15px;font-weight:600;cursor:pointer;transition:background .15s,border-color .15s;}
.btn:hover{background:var(--brand-600);border-color:var(--brand-600);text-decoration:none}
.btn.secondary{background:#fff;color:var(--ink);border-color:var(--line)}
.btn.secondary:hover{background:var(--chip);border-color:#b9c5e8}
.btn.provider{background:#fff;color:var(--ink);border-color:var(--line);font-weight:550}
.btn.provider:hover{background:var(--chip);border-color:#b9c5e8}
.btn.provider img{height:18px;width:18px}
.btn.tiny{width:auto;height:34px;padding:0 14px;font-size:13px;font-weight:550}
.btn.danger{background:#fff;color:var(--danger);border-color:var(--danger-line)}
.btn.danger:hover{background:var(--danger-bg)}
.providers{display:flex;flex-direction:column;gap:10px}
.divider{display:flex;align-items:center;gap:12px;color:var(--muted);font-size:13px;margin:18px 0}
.divider::before,.divider::after{content:"";flex:1;height:1px;background:var(--line)}
.alert{padding:11px 13px;border-radius:12px;font-size:14px;margin:0 0 16px}
.alert.error{background:var(--danger-bg);color:var(--danger);border:1px solid var(--danger-line)}
.alert.ok{background:var(--ok-bg);color:var(--ok);border:1px solid #a7f3d0}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pill.active{background:var(--ok-bg);color:var(--ok)}
.pill.disabled{background:var(--off-bg);color:var(--off)}

/* auth shell — centered on white, no card; generous so it doesn't read as cramped */
.auth{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:56px 24px}
.auth-inner{width:100%;max-width:400px}
.auth .brand{display:flex;justify-content:center;margin-bottom:36px}
.auth .brand img{height:42px}
.consent{text-align:center;margin-bottom:26px}
.consent .small{color:var(--muted);font-size:14px}
.consent .who{font-size:19px;font-weight:600;margin:3px 0}
.foot{margin-top:28px;text-align:center;font-size:14px}

/* admin console shell */
.topbar{height:62px;border-bottom:1px solid var(--line);display:flex;align-items:center;
  justify-content:space-between;padding:0 28px}
.topbar img{height:26px}
.main{max-width:1080px;margin:0 auto;padding:30px 28px 64px}
.head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px}
.tabs{display:flex;gap:6px;margin-top:18px;border-bottom:1px solid var(--line)}
.tabs a{padding:0 14px;height:40px;display:inline-flex;align-items:center;font-size:14px;
  font-weight:550;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}
.tabs a:hover{color:var(--ink);text-decoration:none}
.tabs a.active{color:var(--ink);border-bottom-color:var(--brand)}
.panel{margin-top:24px}
.empty{text-align:center;color:var(--muted);padding:64px 20px;border:1px dashed var(--line);
  border-radius:14px;margin-top:8px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px;min-width:560px}
th{text-align:left;font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;
  letter-spacing:.04em;padding:0 12px 10px}
td{padding:13px 12px;border-top:1px solid var(--line);vertical-align:middle}

/* account menu — an avatar-initials circle with a native <details> dropdown (no JS) */
.usermenu{position:relative}
.usermenu>summary{list-style:none;cursor:pointer;display:inline-flex}
.usermenu>summary::-webkit-details-marker{display:none}
.avatar{height:36px;width:36px;border-radius:999px;background:var(--brand);color:#fff;
  font-size:13px;font-weight:600;letter-spacing:.02em;display:inline-flex;align-items:center;
  justify-content:center;text-transform:uppercase}
.usermenu[open]>summary .avatar{box-shadow:0 0 0 3px rgba(11,87,208,.22)}
.usermenu-pop{position:absolute;right:0;top:46px;width:230px;background:#fff;border:1px solid var(--line);
  border-radius:14px;box-shadow:0 10px 34px rgba(23,23,23,.12);overflow:hidden;z-index:40}
.um-id{padding:13px 15px;border-bottom:1px solid var(--line)}
.um-name{font-weight:600;color:var(--ink)}
.um-email{font-size:12px;color:var(--muted);margin-top:1px;overflow:hidden;text-overflow:ellipsis}
.usermenu-pop a{display:block;padding:11px 15px;color:var(--ink);font-size:14px}
.usermenu-pop a:hover{background:var(--chip);text-decoration:none}

/* Password reveal (eye) toggle — the button is injected inside the field by _PW_SCRIPT. */
.pw-field{position:relative;display:block}
.pw-field>input{padding-right:44px}
.pw-eye{position:absolute;right:6px;top:0;bottom:0;margin:auto;height:34px;width:34px;display:grid;
  place-items:center;padding:0;border:0;background:none;color:var(--muted);cursor:pointer;border-radius:8px}
.pw-eye:hover{background:var(--chip);color:var(--ink)}
.pw-eye.on{color:var(--brand)}
.pw-eye svg{height:18px;width:18px;display:block}

/* CSS-only right drawer (no JS) */
.drawer-toggle{position:absolute;opacity:0;pointer-events:none}
.drawer-wrap{position:fixed;inset:0;z-index:50;pointer-events:none;visibility:hidden}
/* margin:0 is load-bearing — the backdrop is a <label>, and the global `label{margin:16px 0 6px}`
   form-label rule would otherwise push it 16px down, leaving an uncovered strip (the topbar showing
   through) at the top of the overlay. */
.drawer-backdrop{position:absolute;inset:0;margin:0;background:rgba(23,23,23,.32);opacity:0;transition:opacity .22s}
.drawer{position:absolute;top:0;right:0;height:100%;width:430px;max-width:92vw;background:#fff;
  box-shadow:-10px 0 40px rgba(23,23,23,.14);transform:translateX(100%);transition:transform .26s ease;
  padding:26px 28px;overflow:auto}
.drawer-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.drawer-x{cursor:pointer;color:var(--muted);font-size:22px;line-height:1;border:0;background:none}
/* Adjacent-sibling (+), NOT general-sibling (~): each toggle reveals ONLY its immediately-following
   drawer. With the activity tabs' one-drawer-per-row, `~` matched EVERY later drawer — so any row
   opened the same (last) drawer, and the backdrop click toggled the wrong checkbox so it wouldn't
   close. `+` scopes each toggle to its own drawer. */
.drawer-toggle:checked + .drawer-wrap{pointer-events:auto;visibility:visible}
.drawer-toggle:checked + .drawer-wrap .drawer-backdrop{opacity:1}
.drawer-toggle:checked + .drawer-wrap .drawer{transform:translateX(0)}

@media (max-width:560px){
  .auth{padding:32px 18px}
  .topbar{padding:0 16px;height:56px}
  .main{padding:20px 16px 48px}
}

/* — design polish: self-hosted Inter (no font-CDN call) + refined buttons/tables, full-width admin,
   dark code blocks. Appended so these override the base rules above. — */
@font-face{font-family:Inter;font-weight:400;font-display:swap;src:url(/static/fonts/inter-400.woff2) format("woff2")}
@font-face{font-family:Inter;font-weight:500;font-display:swap;src:url(/static/fonts/inter-500.woff2) format("woff2")}
@font-face{font-family:Inter;font-weight:600;font-display:swap;src:url(/static/fonts/inter-600.woff2) format("woff2")}
@font-face{font-family:Inter;font-weight:700;font-display:swap;src:url(/static/fonts/inter-700.woff2) format("woff2")}
:root{--ink:#0e1525;--muted:#69748b;--line:#e7ebf3;--ring:rgba(11,87,208,.18)}
body{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  letter-spacing:-.006em;background:#fbfcfe;color:var(--ink)}
h1{font-weight:660;letter-spacing:-.024em}
.main{max-width:none;padding:30px 40px 64px}
.topbar{height:64px;padding:0 40px;background:#fff;position:sticky;top:0;z-index:30}
.tabs{gap:8px}
.tabs a{font-weight:550;color:var(--muted)}
.tabs a.active{font-weight:650;color:var(--ink)}
.btn{font-weight:600;border-radius:10px;height:42px;box-shadow:0 1px 2px rgba(13,20,38,.07);
  transition:transform .12s ease,box-shadow .18s ease,background .15s}
.btn:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(11,87,208,.24)}
.btn.tiny{height:30px;border-radius:8px;font-weight:550;box-shadow:none}
.btn.tiny:hover{transform:none;box-shadow:0 1px 2px rgba(13,20,38,.08)}
.btn.secondary{background:#fff;color:var(--ink);border-color:var(--line)}
.btn.secondary:hover{background:#f4f7fe;border-color:#cdd8ee}
table{font-size:13.5px}
thead th{font-size:11px;letter-spacing:.07em;color:#8a94a8;padding-bottom:12px}
td{padding:15px 12px;border-top:1px solid var(--line)}
tbody tr:hover{background:#f6f9ff}
.pill{font-weight:600;letter-spacing:.01em;padding:3px 11px}
pre.code{background:#0e1525;color:#e8edf7;border:0;border-radius:12px;line-height:1.6}
input,select{border-radius:10px;border-color:var(--line)}
input:focus,select:focus{border-color:var(--brand);box-shadow:0 0 0 4px var(--ring)}
.drawer{box-shadow:-26px 0 70px rgba(13,20,38,.20);border-left:1px solid var(--line)}
time{font-variant-numeric:tabular-nums}
@media (max-width:680px){.main{padding:18px 16px 48px}.topbar{padding:0 16px}.drawer{width:94vw!important}}

/* ---- read-only model explorer (catalog idiom: browse tree + one page at a time) ---- */
.explorer{display:grid;grid-template-columns:248px 1fr;align-items:start;margin:-4px 0 0}
.tree{border-right:1px solid var(--line);padding:6px 14px 24px;font-size:13.5px}
.tree h4{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#8a94a8;margin:16px 8px 6px}
.ds{display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--line);border-radius:9px;background:#f7f9fc;font-weight:600;margin-bottom:8px}
.ds select{border:0;background:transparent;font:inherit;font-weight:600;color:var(--ink);outline:none;width:100%;height:auto}
.ds-go{flex:none;font:inherit;font-size:12px;font-weight:600;color:var(--brand-600);background:#fff;border:1px solid var(--line);border-radius:7px;padding:3px 9px;cursor:pointer}
.ds-go:hover{background:#eef3fe;border-color:#cfe0fb}
.navitem{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 10px;border-radius:8px;color:var(--ink);text-decoration:none;font-weight:550}
.navitem:hover{background:#f1f5fd;text-decoration:none}
.navitem.active{background:#eef3fe;color:var(--brand-600)}
.navitem .n{font-size:11px;color:#8a94a8;background:#eef1f7;border-radius:20px;padding:1px 8px;font-weight:600}
.navitem.active .n{background:#dde9fd;color:var(--brand-600)}
.children{margin:1px 0 6px 14px;border-left:1px solid var(--line);padding-left:6px;display:flex;flex-direction:column}
.leaf{padding:6px 9px;border-radius:7px;color:var(--muted);text-decoration:none;font-size:13px}
.leaf:hover{background:#f1f5fd;color:var(--ink);text-decoration:none}
.leaf.active{background:#eef3fe;color:var(--brand-600);font-weight:600}
.content{padding:6px 4px 40px 30px;min-width:0}
.content .crumbs{font-size:12.5px;color:var(--muted);margin-bottom:8px}
.content .crumbs a{color:var(--muted)}
.content .crumbs a:hover{color:var(--brand)}
.content .sep{color:#c2cadb;margin:0 7px}
.h1row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.h1row h1{font-family:ui-monospace,Menlo,monospace;font-size:21px;font-weight:680}
.h1row h1 .schema{color:#9aa6bd;font-weight:500}
.lead{color:var(--muted);max-width:760px;margin:6px 0 0}
.readonly-pill{margin-left:auto;font-size:11px;font-weight:600;color:var(--muted);background:#f1f3f9;border:1px solid var(--line);border-radius:20px;padding:4px 11px}
.subline{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin:10px 0 0}
.subline b{color:var(--ink);font-weight:600}
.desc{margin:14px 0 0;color:#33415c;max-width:760px}
.descsrc{font-size:11px;color:#b45309;background:#fffbeb;border:1px solid #fde68a;border-radius:20px;padding:1px 8px;font-weight:600;margin-left:6px}
h2.sec{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#8a94a8;margin:30px 0 12px;font-weight:650}
h2.sec .c{background:#eef1f7;color:#8a94a8;border-radius:20px;padding:1px 9px;font-size:12px;margin-left:6px}
.statrow{display:flex;flex-wrap:wrap;gap:26px;align-items:center;margin:16px 0 0;padding:14px 18px;background:#fff;border:1px solid var(--line);border-radius:12px}
.stat .k{font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#8a94a8}
.stat .v{font-size:20px;font-weight:680}
.gloss{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.term{background:#f4f6fb;border:1px solid var(--line);border-radius:8px;padding:5px 10px;font-size:12.5px}
.tlist{background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.trow{display:flex;align-items:center;gap:14px;padding:13px 16px;border-bottom:1px solid var(--line);text-decoration:none;color:inherit}
.trow:last-child{border-bottom:0}
.trow:hover{background:#f6f9ff;text-decoration:none}
.trow .nm{font-weight:600;font-family:ui-monospace,Menlo,monospace;font-size:13.5px;min-width:150px}
.trow .d{color:var(--muted);flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trow .meta{color:#9aa6bd;font-size:12px;white-space:nowrap}
.trow .chev{color:#c2cadb}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.mcard{background:#fff;border:1px solid var(--line);border-radius:11px;padding:13px 15px}
.mcard .nm{font-weight:600}
.mcard .al{color:#9aa6bd;font-weight:500;font-size:12px}
.calc{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#0b3b8c;background:#f5f8ff;border:1px solid var(--line);border-radius:7px;padding:4px 8px;display:inline-block;margin-top:7px}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;border:1px solid transparent;white-space:nowrap}
.b-confirmed{color:#047857;background:#ecfdf5;border-color:#a7f3d0}
.b-inferred{color:var(--brand-600);background:#eef3fe;border-color:#cfe0fb}
.b-proposed{color:#b45309;background:#fffbeb;border-color:#fde68a}
.b-sensitive{color:#b42318;background:#fef3f2;border-color:#fecdca}
.b-pk{color:#6d28d9;background:#f5f3ff;border-color:#ddd6fe}
.b-fk{color:#0369a1;background:#f0f9ff;border-color:#bae6fd}
.b-soft{color:var(--muted);background:#f1f3f9;border-color:var(--line)}
.aichip{font-size:10px;font-weight:700;color:#b45309;background:#fffbeb;border:1px solid #fde68a;border-radius:5px;padding:0 4px;vertical-align:middle}
.caveat{display:flex;gap:11px;background:#fffbeb;border:1px solid #fde68a;border-radius:11px;padding:13px 15px;margin:18px 0}
.caveat .ic{color:#b45309;font-weight:700;flex:none}
.caveat .t{color:#7c4a02;font-size:13px}
.caveat .t b{color:#5c3700}
table.cols{width:100%;border-collapse:collapse;font-size:13.5px;margin:0}
.cols th{text-align:left;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:#8a94a8;font-weight:600;padding:11px 12px;border-top:0;border-bottom:1px solid var(--line)}
.cols td{padding:11px 12px;border-top:0;border-bottom:1px solid var(--line);vertical-align:top}
.cols tr:last-child td{border-bottom:0}
.cols tbody tr:hover{background:#f8fafe}
.cols .noterow td{border-bottom:0;padding-top:0}
.cn{font-weight:600;font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.ct{display:inline-block;color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:11.5px;background:#f7f9fc;border:1px solid var(--line);border-radius:6px;padding:1px 7px}
.cd{color:#445}
.cd .dash{color:#c2cadb}
.note{font-size:12.5px;color:#7c4a02;background:#fffbeb;border-left:3px solid #fde68a;padding:8px 12px;border-radius:0 7px 7px 0}
details.grp{background:#fff;border:1px solid var(--line);border-radius:11px;margin:12px 0;overflow:hidden}
details.grp>summary{list-style:none;cursor:pointer;padding:12px 16px;display:flex;align-items:center;gap:10px}
details.grp>summary::-webkit-details-marker{display:none}
details.grp>summary:hover{background:#f7f9fc}
details.grp .gname{font-weight:680;font-size:13.5px}
details.grp .gdesc{color:var(--muted);font-size:12.5px}
details.grp .gn{margin-left:auto;font-size:11px;color:#8a94a8;background:#eef1f7;border:1px solid var(--line);border-radius:20px;padding:1px 9px}
details.grp .cols,details.grp>.rel:first-of-type{border-top:1px solid var(--line)}
details.grp>.rel{padding-left:18px;padding-right:18px}
details.showmore>summary{cursor:pointer;color:var(--brand);font-size:12.5px;font-weight:600;padding:11px 12px;list-style:none}
details.showmore>summary::-webkit-details-marker{display:none}
.rel{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line)}
.rel:last-child{border-bottom:0}
.rel .arr{color:var(--brand);font-weight:700}
.rel .ro{margin-left:auto;color:#9aa6bd;font-size:12px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.context{background:#fff;border:1px solid var(--line);border-radius:12px;padding:6px 24px 20px;margin-top:8px}
.context h1{font-size:18px;margin:18px 0 6px}
.context h2{font-size:15px;margin:16px 0 6px}
.context h3{font-size:13.5px;margin:14px 0 6px}
.context p,.context li{color:#33415c;font-size:14px}
.context code{font-family:ui-monospace,Menlo,monospace;background:#f1f3f9;border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:12.5px}
.context pre.code{padding:12px 14px;white-space:pre-wrap}
@media (max-width:820px){.explorer{grid-template-columns:1fr}.tree{border-right:0;border-bottom:1px solid var(--line)}.content{padding:16px 0 40px}.grid{grid-template-columns:1fr}}
"""


def esc(value: str | None) -> str:
    """HTML-escape (attribute-safe) any interpolated value. Use for EVERYTHING user-influenced."""
    return html.escape(value or "", quote=True)


def _md_inline(s: str) -> str:
    """Inline markdown on an ALREADY-escaped string: inline code, bold, italic, and scheme-checked
    links. Inline-code spans are pulled out FIRST and restored LAST, so their contents stay literal —
    a `[x](url)` or `**y**` inside backticks is never re-parsed into a live link / bold."""
    spans: list[str] = []

    def _stash(m: "re.Match[str]") -> str:
        spans.append(m.group(1))
        # NUL can't appear in html.escape'd text, so the placeholder can't collide with real content.
        return f"\x00{len(spans) - 1}\x00"

    s = re.sub(r"`([^`]+)`", _stash, s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<em>\1</em>", s)

    def _link(m: "re.Match[str]") -> str:
        text, url = m.group(1), m.group(2)
        # Only http(s) links — the text is already escaped, so a `javascript:`/`data:` URL renders as
        # plain text (never a live href). This is the one place a URL becomes an attribute.
        if url.startswith(("http://", "https://")):
            return f'<a href="{url}" rel="noopener noreferrer" target="_blank">{text}</a>'
        return text

    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", _link, s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{spans[int(m.group(1))]}</code>", s)


def md(text: str) -> str:
    """A tiny, SAFE markdown subset for the domain-context doc — **escape-first**, so any raw HTML in
    the source is inert. Supports headings, bold/italic, inline + fenced code, bullet/numbered lists,
    and http(s) links. Not a full renderer; just enough for an datasource.md."""
    if not text:
        return ""
    out: list[str] = []
    lst: str | None = None  # the open list tag ("ul"/"ol"), or None
    para: list[str] = []
    code: list[str] | None = None  # accumulating fenced-code lines, or None

    def _flush_para() -> None:
        if para:
            out.append(f"<p>{_md_inline(' '.join(para))}</p>")
            para.clear()

    def _close_list() -> None:
        nonlocal lst
        if lst:
            out.append(f"</{lst}>")
            lst = None

    for raw in text.splitlines():
        line = html.escape(raw)
        if line.strip() == "```" or line.strip().startswith("```"):
            if code is None:  # opening a fence
                _flush_para()
                _close_list()
                code = []
            else:  # closing it
                out.append(f'<pre class="code">{chr(10).join(code)}</pre>')
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            _flush_para()
            _close_list()
            lvl = len(h.group(1))
            out.append(f"<h{lvl}>{_md_inline(h.group(2))}</h{lvl}>")
            continue
        m = re.match(r"^\s*([-*]|\d+\.)\s+(.*)$", line)
        if m:
            _flush_para()
            want = "ol" if m.group(1)[0].isdigit() else "ul"
            if lst != want:
                _close_list()
                out.append(f"<{want}>")
                lst = want
            out.append(f"<li>{_md_inline(m.group(2))}</li>")
            continue
        if not line.strip():
            _flush_para()
            _close_list()
            continue
        para.append(line.strip())
    if code is not None:  # an unterminated fence still renders (no data dropped)
        out.append(f'<pre class="code">{chr(10).join(code)}</pre>')
    _flush_para()
    _close_list()
    return "\n".join(out)


def initials(name: str) -> str:
    """Up to two leading-letter initials for the avatar (falls back to '?' for an empty name)."""
    parts = [p for p in (name or "").split() if p]
    letters = "".join(p[0] for p in parts[:2])
    return letters.upper() or "?"


# Localize <time data-utc="…"> to the viewer's timezone — the server stores UTC and can't know the
# browser's zone, so this is the one small script. No-JS degrades to the UTC text already in the element.
_TIME_SCRIPT = (
    "<script>for(const t of document.querySelectorAll('time[data-utc]')){"
    "const d=new Date(t.dataset.utc);"
    "if(!isNaN(d))t.textContent=d.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});}"
    "</script>"
)

# Add a reveal (eye) toggle to every password field. Toggling an input's `type` needs a line of JS
# (CSS can't do it); with no JS the field still works as a normal password input. Wraps each input so
# the button sits inside it — applies to all sign-in pages (admin / OAuth / onboarding) at once.
_PW_SCRIPT = (
    "<script>for(const i of document.querySelectorAll('input[type=password]')){"
    "const w=document.createElement('span');w.className='pw-field';"
    "i.parentNode.insertBefore(w,i);w.appendChild(i);"
    "const b=document.createElement('button');b.type='button';b.className='pw-eye';"
    "b.setAttribute('aria-label','Show password');"
    "b.innerHTML='<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\""
    " stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10"
    " 7-10-7-10-7Z\"/><circle cx=\"12\" cy=\"12\" r=\"3\"/></svg>';"
    "b.addEventListener('click',function(){var s=i.type==='password';i.type=s?'text':'password';"
    "b.classList.toggle('on',s);b.setAttribute('aria-label',s?'Hide password':'Show password');});"
    "w.appendChild(b);}</script>"
)


def _doc(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="icon" href="/static/logo_icon.png">
<style>{_CSS}</style>
</head><body>{body}{_TIME_SCRIPT}{_PW_SCRIPT}</body></html>"""


def auth_page(title: str, body: str) -> str:
    """Centered sign-in / consent shell: the agami logo above `body`, on plain white."""
    return _doc(
        title,
        f'<div class="auth"><div class="auth-inner">'
        f'<div class="brand"><img src="/static/logo_h.svg" alt="agami"></div>'
        f"{body}</div></div>",
    )


_PROVIDER_LABELS = {"google": "Google", "microsoft": "Microsoft"}


def provider_button(key: str, href: str) -> str:
    """A pill 'Continue with <provider>' button (provider icon + label)."""
    label = _PROVIDER_LABELS.get(key, key.title())
    return (
        f'<a class="btn provider" href="{esc(href)}">'
        f'<img src="/static/{esc(key)}_logo.svg" alt=""> Continue with {esc(label)}</a>'
    )


_TABS = (
    ("dashboard", "Dashboard"),
    ("users", "Users"),
    ("activity", "Activity"),
    ("model", "Model"),
)

# Most tabs hang off `/admin?tab=`; the read-only Model view has its own contract-named GET endpoint
# (`/admin/model`) so it's the *only* model surface — no write route can hide behind a shared handler.
_TAB_HREFS = {"model": "/admin/model"}


def _tab_href(key: str) -> str:
    return _TAB_HREFS.get(key, f"/admin?tab={esc(key)}")


def _account_menu(label: str, email: str) -> str:
    """The top-right avatar + dropdown (signed-in identity + Sign out), native <details>, no JS."""
    return f"""<details class="usermenu">
<summary aria-label="Account menu"><span class="avatar">{esc(initials(label))}</span></summary>
<div class="usermenu-pop">
<div class="um-id"><div class="um-name">{esc(label)}</div><div class="um-email">{esc(email)}</div></div>
<a href="/admin/logout">Sign out</a>
</div></details>"""


def admin_shell(
    title: str,
    active: str,
    body: str,
    *,
    admin_label: str = "",
    admin_email: str = "",
    extra: str = "",
) -> str:
    """The admin console shell: a top bar (logo + account menu) and the Dashboard/Users/Activity tabs.
    `extra` is emitted at the body root before the bar — used for the CSS-only drawer (whose toggle
    checkbox must be the **immediately-preceding** sibling of its `.drawer-wrap`, per the `+` rule)."""
    tabs = "".join(
        f'<a href="{_tab_href(key)}" class="{"active" if key == active else ""}">{esc(label)}</a>'
        for key, label in _TABS
    )
    body_html = f"""{extra}<div class="topbar">
<img src="/static/logo_h.svg" alt="agami">
{_account_menu(admin_label or "Admin", admin_email)}
</div>
<div class="main">
<div class="head"><h1>Admin Console</h1></div>
<nav class="tabs">{tabs}</nav>
<div class="panel">{body}</div>
</div>"""
    return _doc(title, body_html)
