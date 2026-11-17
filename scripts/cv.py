#!/usr/bin/env python3
"""Google Docs CV -> static, SEO-friendly website.

    python scripts/cv.py fetch   # download the Google Doc HTML export into source/
    python scripts/cv.py build   # source/ -> content/ (cv.json + photo) -> _site/

`build` works without `fetch`: if source/ is missing it renders the committed content/.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "source"
CONTENT = ROOT / "content"
SITE = ROOT / "_site"
CV_JSON = CONTENT / "cv.json"
PHOTO = CONTENT / "photo.jpg"


def load_config() -> dict:
    with open(ROOT / "site.toml", "rb") as f:
        return tomllib.load(f)


# --------------------------------------------------------------------------- fetch

def doc_id_from(value: str) -> str:
    value = value.strip()
    m = re.search(r"/document/d/([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value


def cmd_fetch(_args) -> None:
    cfg = load_config()
    raw = os.environ.get("GOOGLE_DOC_ID") or cfg.get("google_doc_id") or ""
    if not raw.strip():
        print("No Google Doc configured (GOOGLE_DOC_ID / site.toml) - using committed content/.")
        return
    doc_id = doc_id_from(raw)
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=zip"
    req = urllib.request.Request(url, headers={"User-Agent": "cv-site-builder"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if not data.startswith(b"PK"):
        sys.exit(
            "Google did not return a zip export. Make sure the doc is shared as "
            "'Anyone with the link -> Viewer' and the ID is correct."
        )
    if SOURCE.exists():
        shutil.rmtree(SOURCE)
    SOURCE.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            target = (SOURCE / member.filename).resolve()
            if not str(target).startswith(str(SOURCE.resolve())):
                continue  # ignore path traversal entries
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member))
    # The .docx export tells which paragraphs set their own spacing (see docx_spacing).
    req = urllib.request.Request(url.replace("format=zip", "format=docx"), headers={"User-Agent": "cv-site-builder"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        docx = resp.read()
    if docx.startswith(b"PK"):
        (SOURCE / "doc.docx").write_bytes(docx)
    print(f"Fetched Google Doc {doc_id}: {len(data)} bytes html, {len(docx)} bytes docx")


def norm_text(s: str) -> str:
    return re.sub(r"\s+", "", s)


def docx_spacing(path: Path) -> list[dict]:
    """Space before/after and line spacing of every paragraph, as Google Docs renders them.

    Docs draws a paragraph with no explicit space before/after with 0, even when the
    "Normal text" style has some; the HTML export wrongly applies the style value instead.
    The .docx export only writes w:spacing before/after when the paragraph really sets it.
    """
    with zipfile.ZipFile(path) as z:
        doc = z.read("word/document.xml").decode("utf-8")
        styles = z.read("word/styles.xml").decode("utf-8")

    def attrs(tag: str) -> dict:
        return dict(re.findall(r'w:(\w+)="([^"]*)"', tag))

    def line_of(tag: str | None) -> float | None:
        a = attrs(tag or "")
        if "line" in a and a.get("lineRule", "auto") == "auto":
            return int(a["line"]) / 240
        return None

    defaults = re.search(r"<w:docDefaults>.*?</w:docDefaults>", styles, re.S)
    sp = re.search(r"<w:spacing\b[^>]*/>", defaults.group(0)) if defaults else None
    default_line = line_of(sp.group(0) if sp else None) or 1.15
    style_line = {}
    for m in re.finditer(r'<w:style\b[^>]*w:styleId="([^"]+)".*?</w:style>', styles, re.S):
        sp = re.search(r"<w:spacing\b[^>]*/>", m.group(0))
        if sp and line_of(sp.group(0)):
            style_line[m.group(1)] = line_of(sp.group(0))

    out = []
    for p in re.findall(r"<w:p\b[^>]*>.*?</w:p>", doc, re.S):
        ppr = re.search(r"<w:pPr>(.*?)</w:pPr>", p, re.S)
        ppr = re.sub(r"<w:(rPr|pPrChange)>.*?</w:\1>", "", ppr.group(1), flags=re.S) if ppr else ""
        sp = re.search(r"<w:spacing\b[^>]*/>", ppr)
        a = attrs(sp.group(0)) if sp else {}
        style = re.search(r'<w:pStyle w:val="([^"]+)"', ppr)
        line = line_of(sp.group(0) if sp else None) or style_line.get(style.group(1) if style else "Normal", default_line)
        text = html.unescape("".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", p)))
        out.append({"key": norm_text(text), "before": int(a.get("before", 0)) / 20,
                    "after": int(a.get("after", 0)) / 20, "line": line})
    return [o for o in out if o["key"]]


# --------------------------------------------------------------------------- parse

def parse_docs_export(html_path: Path, docx_path: Path | None = None) -> tuple[dict, Path | None]:
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    css = soup.style.string if soup.style and soup.style.string else ""
    bold_cls = set(re.findall(r"\.(c\d+)\{[^}]*font-weight:700", css))
    ital_cls = set(re.findall(r"\.(c\d+)\{[^}]*font-style:italic", css))

    # Docs exports per-paragraph spacing (space before/after, line spacing, alignment) as CSS
    # classes; collect them so the site reuses the spacing set manually in the Doc.
    rules: dict[str, dict] = {}
    for sel, decl in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        props = dict((k.strip(), v.strip()) for k, v in re.findall(r"([a-z-]+):([^;]+)", decl))
        for one in sel.split(","):
            one = one.strip()
            if re.fullmatch(r"\.c\d+|h[1-6]|p", one):
                rules.setdefault(one, {}).update(props)

    def pt(v) -> float:
        m = re.match(r"(-?[\d.]+)pt", v or "")
        return float(m.group(1)) if m else 0.0

    spacing = docx_spacing(docx_path) if docx_path and docx_path.exists() else []
    cursor = 0

    def docx_match(el) -> dict | None:
        """Find this HTML paragraph in the .docx export (same order, matched by text)."""
        nonlocal cursor
        key = norm_text(el.get_text())
        if not key:
            return None
        for j in range(cursor, min(cursor + 12, len(spacing))):
            if spacing[j]["key"] == key:
                cursor = j + 1
                return spacing[j]
        return None

    def doc_style(el) -> dict:
        props = dict(rules.get(el.name, {}))
        for c in el.get("class", []):
            props.update(rules.get("." + c, {}))
        m = docx_match(el)
        if m:  # trust the .docx for spacing: it matches what Docs renders
            props["padding-top"], props["padding-bottom"] = f"{m['before']}pt", f"{m['after']}pt"
            props["line-height"] = f"{m['line']:g}"
        size = pt(props.get("font-size")) or pt(rules.get("p", {}).get("font-size")) or 11.0
        st = {}
        if pt(props.get("padding-top")):
            st["pt"] = round(pt(props["padding-top"]) / size, 3)  # em of the element's font size
        if pt(props.get("padding-bottom")):
            st["pb"] = round(pt(props["padding-bottom"]) / size, 3)
        if re.fullmatch(r"[\d.]+", props.get("line-height", "")):
            st["lh"] = float(props["line-height"])
        if props.get("text-align") in ("justify", "center", "right"):
            st["align"] = props["text-align"]
        return st

    def real_url(href: str) -> str:
        u = urlparse(href)
        if u.netloc.endswith("google.com") and u.path == "/url":
            q = parse_qs(u.query).get("q")
            if q:
                return q[0]
        return href

    def tidy(s: str) -> str:
        return re.sub(r"[ \t ]{2,}", " ", s.replace(" ", " ")).strip()

    def inline(node, allow_bold=True) -> str:
        out = []
        for ch in node.children:
            if isinstance(ch, NavigableString):
                out.append(html.escape(str(ch), quote=False))
            elif isinstance(ch, Tag):
                if ch.name == "a" and ch.get("href"):
                    out.append(f'<a href="{html.escape(real_url(ch["href"]))}">{html.escape(ch.get_text(), quote=False)}</a>')
                elif ch.name in ("img", "br"):
                    continue
                elif ch.name == "span":
                    cls = set(ch.get("class", []))
                    inner = inline(ch, allow_bold)
                    if allow_bold and cls & bold_cls and inner.strip():
                        inner = f"<strong>{inner}</strong>"
                    if cls & ital_cls and inner.strip():
                        inner = f"<em>{inner}</em>"
                    out.append(inner)
                else:
                    out.append(inline(ch, allow_bold))
        return re.sub(r"[ \t]{2,}", " ", "".join(out).replace("\u00a0", " "))

    def text_of(tag) -> str:
        return tidy(tag.get_text())

    sections: list[dict] = []
    sec: dict | None = None
    item: dict | None = None

    def blocks() -> list:
        return item["blocks"] if item else sec["intro"]

    def add_paragraph(p, as_list_item=False):
        raw = text_of(p)
        if not raw:
            return
        links = p.find_all("a")
        tech_m = re.search(r"🔧\s*(.+)$", raw)
        rest = raw
        for a in links:
            rest = rest.replace(tidy(a.get_text()), "")
        if tech_m:
            rest = rest.replace(tech_m.group(0), "")
        target = blocks()
        st = doc_style(p)
        n_before = len(target)
        if links and not rest.strip():
            target.append({"type": "links", "style": st, "links": [
                {"label": tidy(a.get_text()), "url": real_url(a["href"])} for a in links if tidy(a.get_text())
            ]})
        elif as_list_item:
            if target and target[-1]["type"] == "list":
                target[-1]["items"].append(tidy(inline(p)))
            else:
                target.append({"type": "list", "style": st, "items": [tidy(inline(p))]})
        elif not tech_m:
            target.append({"type": "p", "style": st, "html": tidy(inline(p))})
        if tech_m:
            tags = [t.strip() for t in re.split(r"[,;]", tech_m.group(1)) if t.strip()]
            if len(target) > n_before:  # same Doc paragraph split in two: space before goes to the first part
                tst = {k: v for k, v in st.items() if k != "pt"}
                target[-1]["style"] = {k: v for k, v in st.items() if k != "pb"}
            else:
                tst = st
            target.append({"type": "tech", "style": tst, "tags": tags})

    def add_heading(h):
        nonlocal item
        a = h.find("a")
        t = text_of(h)
        # A heading that is only a link, right after an item with no body = that item's link.
        if a and t == tidy(a.get_text()) and item and not item["blocks"] and item.get("meta"):
            item["blocks"].append({"type": "links", "style": doc_style(h), "links": [{"label": t, "url": real_url(a["href"])}]})
            return
        title_html, meta, kind = tidy(inline(h, allow_bold=False)), None, None
        if " | " in t:
            title, meta = (s.strip() for s in t.rsplit(" | ", 1))
            title_html, kind = html.escape(title, quote=False), "date"
        else:
            m = re.match(r"^(.*?)\s*[—–]\s*((?:19|20)\d\d\b.*)$", t)
            if m:
                title_html, meta, kind = html.escape(m.group(1).strip(), quote=False), m.group(2).strip(), "result"
        item = {"title_html": title_html, "meta": meta, "meta_kind": kind, "style": doc_style(h), "blocks": []}
        sec["items"].append(item)

    for el in soup.body.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name == "h1" and text_of(el):
            sec = {"title": text_of(el), "style": doc_style(el), "intro": [], "items": []}
            sections.append(sec)
            item = None
        elif sec is None:
            continue
        elif el.name in ("h2", "h3", "h4"):
            if text_of(el):
                add_heading(el)
        elif el.name == "p" and not el.find_parent(["h1", "h2", "h3", "h4", "li"]):
            add_paragraph(el)
        elif el.name == "li":
            add_paragraph(el, as_list_item=True)

    for s in sections:
        s["id"] = re.sub(r"[^a-z0-9]+", "-", s["title"].lower()).strip("-") or "section"
        s["is_summary"] = bool(re.fullmatch(r"(summary|about( me)?|profile|обо мне|о себе)", s["title"].strip().lower()))
        if s["is_summary"]:  # flatten: summary is shown in the page header
            s["intro"] += [b for it in s.pop("items") for b in it["blocks"]]
            s["items"] = []

    img = soup.body.find("img")
    photo = (html_path.parent / img["src"]) if img and img.get("src") else None
    content = {"sections": sections}
    if img is not None:
        crop = photo_crop(img)
        if crop:
            content["photo_crop"] = crop
    return content, (photo if photo and photo.is_file() else None)


def photo_crop(img) -> list[float] | None:
    """Crop box (fractions l, t, r, b) the Doc applies to the photo via an overflow:hidden frame."""
    def px(style: str, prop: str) -> float | None:
        m = re.search(rf"(?:^|;)\s*{prop}:\s*(-?[\d.]+)px", style or "")
        return float(m.group(1)) if m else None

    frame = img.parent.get("style", "") if img.parent else ""
    st = img.get("style", "")
    iw, ih = px(st, "width"), px(st, "height")
    fw, fh = px(frame, "width"), px(frame, "height")
    if not (iw and ih and fw and fh) or "overflow: hidden" not in frame:
        return None
    ml, mt = -(px(st, "margin-left") or 0.0), -(px(st, "margin-top") or 0.0)
    box = [ml / iw, mt / ih, (ml + fw) / iw, (mt + fh) / ih]
    box = [round(min(max(v, 0.0), 1.0), 4) + 0.0 for v in box]
    return None if box == [0.0, 0.0, 1.0, 1.0] else box


def cmd_parse() -> None:
    htmls = sorted(SOURCE.glob("*.html")) if SOURCE.exists() else []
    if not htmls:
        print("No source/*.html - keeping committed content/.")
        return
    content, photo_src = parse_docs_export(htmls[0], SOURCE / "doc.docx")
    CONTENT.mkdir(exist_ok=True)

    old = json.loads(CV_JSON.read_text(encoding="utf-8")) if CV_JSON.exists() else {}
    if photo_src:
        digest = hashlib.sha256(photo_src.read_bytes()).hexdigest()
        content["photo_sha256"] = digest
        crop = content.get("photo_crop")
        if old.get("photo_sha256") != digest or old.get("photo_crop") != crop or not PHOTO.exists():
            from PIL import Image, ImageOps

            im = ImageOps.exif_transpose(Image.open(photo_src)).convert("RGB")
            if crop:
                w, h = im.size
                im = im.crop((round(crop[0] * w), round(crop[1] * h), round(crop[2] * w), round(crop[3] * h)))
            im.thumbnail((640, 800))
            im.save(PHOTO, "JPEG", quality=85, optimize=True, progressive=True)
    elif "photo_sha256" in old:
        content["photo_sha256"] = old["photo_sha256"]
        if "photo_crop" in old:
            content["photo_crop"] = old["photo_crop"]

    comparable = lambda d: {k: v for k, v in d.items() if k != "updated"}
    if comparable(old) == comparable(content) and old.get("updated"):
        content["updated"] = old["updated"]
    else:
        content["updated"] = dt.date.today().isoformat()
        print("CV content changed - updated:", content["updated"])
    CV_JSON.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- render

CSS = r"""
:root{--bg:#fff;--ink:#1d1d1f;--body:#2b2b2e;--muted:#5f6368;--line:#e6e6e9;--soft:#f5f5f7;
--accent:#1155cc;--accent-soft:#e8f0fe;--win:#137333;--win-soft:#e6f4ea;--fin:#8a5a00;--fin-soft:#fef3d8;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--ink:#f2f2f4;--body:#d6d6da;--muted:#a1a1aa;--line:#2a2a2e;
--soft:#1c1c20;--accent:#8ab4f8;--accent-soft:#1b2a44;--win:#7fd99a;--win-soft:#16301f;--fin:#f0c76b;--fin-soft:#33290f;color-scheme:dark}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 "Lato",system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.cv{max-width:880px;margin:0 auto;padding:40px 20px 56px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.hero{text-align:center;margin-bottom:22px}
h1{font:800 34px/1.15 "Raleway",system-ui,sans-serif;margin:0 0 6px;letter-spacing:.03em;text-transform:uppercase}
.hero .role{font-size:17px;color:var(--ink);margin:0 0 10px;text-align:center;hyphens:none;line-height:1.35}
.contacts{list-style:none;display:flex;flex-wrap:wrap;justify-content:center;gap:4px 0;padding:0;margin:0;font-size:15px}
.contacts li+li::before{content:"|";color:var(--muted);margin:0 10px}
.contacts a{text-decoration:underline;text-underline-offset:2px}
.contacts .short{display:none}
.nw{white-space:nowrap}
.summary .photo{float:right;width:260px;height:auto;margin:4px 0 12px 24px;border-radius:6px;background:var(--soft)}
.summary::after{content:"";display:block;clear:both}
.layout{display:grid;grid-template-columns:minmax(0,1fr)}
.sidebar ul{list-style:none;margin:0;padding:0}
.sidebar a{font:700 14px/1.3 "Raleway",system-ui,sans-serif;color:var(--muted);white-space:nowrap}
.sidebar a:hover{color:var(--ink);text-decoration:none}
/* desktop: sticky sidebar, first item lined up with the Summary heading (offset set by JS) */
@media (min-width:960px){.layout{grid-template-columns:190px minmax(0,880px);justify-content:center;column-gap:8px}
.sidebar nav{position:sticky;top:24px;margin-top:var(--nav-offset,150px);padding:0 0 24px 20px;max-height:calc(100vh - 24px);overflow-y:auto}
.sidebar ul{border-left:2px solid var(--line)}
.sidebar a{display:block;margin-left:-2px;padding:7px 14px;border-left:2px solid transparent;white-space:normal}
.sidebar a.active{color:var(--accent);border-left-color:var(--accent)}}
@media (min-width:1300px){.layout{grid-template-columns:1fr minmax(0,880px) 1fr;column-gap:0}.sidebar{justify-self:end;width:200px}}
/* phones / tablets: the same nav as a top bar of chips that slides in once the header is scrolled past */
@media (max-width:959px){
.sidebar nav{position:fixed;top:0;left:0;right:0;z-index:10;background:var(--bg);border-bottom:1px solid var(--line);
box-shadow:0 2px 10px rgba(0,0,0,.06);transform:translateY(-110%);transition:transform .25s ease}
body.nav-on .sidebar nav{transform:none}
.sidebar ul{display:flex;gap:6px;overflow-x:auto;padding:8px 16px;scrollbar-width:none;-webkit-overflow-scrolling:touch}
.sidebar ul::-webkit-scrollbar{display:none}
.sidebar a{display:block;padding:7px 12px;border-radius:999px;background:var(--soft);font-size:13px}
.sidebar a.active{background:var(--accent-soft);color:var(--accent)}
section{scroll-margin-top:58px}}
.totop{display:none}
@media (max-width:959px){.totop{display:flex;align-items:center;justify-content:center;position:fixed;right:16px;bottom:18px;z-index:10;
width:46px;height:46px;border:0;border-radius:50%;background:var(--accent);color:#fff;cursor:pointer;
box-shadow:0 4px 14px rgba(0,0,0,.2);opacity:0;transform:translateY(12px);pointer-events:none;transition:opacity .2s,transform .2s}
body.totop-on .totop{opacity:1;transform:none;pointer-events:auto}}
@media (prefers-color-scheme:dark){.totop{color:#0d1b33}}
section{scroll-margin-top:12px}
h2{font:800 22px/1.2 "Raleway",system-ui,sans-serif;margin:0;display:flex;align-items:center;gap:10px}
h2::before{content:"";width:5px;height:.95em;border-radius:3px;background:var(--accent)}
.item header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:2px 14px}
h3{font:700 17px/1.2 "Raleway",system-ui,sans-serif;margin:0;flex:1 1 380px}
.date{color:var(--muted);font-size:14px;white-space:nowrap}
.badge{font:700 12.5px/1 "Lato",sans-serif;padding:6px 10px;border-radius:999px;background:var(--soft);color:var(--muted);white-space:nowrap}
.badge.win{background:var(--win-soft);color:var(--win)}
.badge.fin{background:var(--fin-soft);color:var(--fin)}
.cv p{margin:0;color:var(--body);line-height:1.2;text-align:justify;hyphens:auto;-webkit-hyphens:auto}
.item ul.bullets{margin:0;padding-left:20px;color:var(--body);text-align:justify;hyphens:auto;-webkit-hyphens:auto}
.links{display:flex;flex-wrap:wrap;gap:2px 16px;margin:0;font-size:15px;line-height:1.3}
.links a::after{content:" ↗";font-size:.85em}
.tech{list-style:none;display:flex;flex-wrap:wrap;gap:6px;margin:0;padding-left:0;margin-top:4px}
.tech li{font-size:12.5px;line-height:1;padding:6px 9px;border-radius:6px;background:var(--soft);color:var(--muted);border:1px solid var(--line)}
footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:13.5px;display:flex;flex-wrap:wrap;gap:6px 18px;justify-content:space-between}
@media (max-width:640px){.cv{padding:24px 16px 40px}.summary .photo{width:42%;margin-left:14px}
h1{font-size:27px}.hero .role{font-size:15.5px;line-height:1.35;margin-bottom:12px}
.contacts{gap:6px;font-size:13.5px;flex-wrap:nowrap}.contacts li+li::before{content:none}
.contacts .full{display:none}.contacts .short{display:inline}
.contacts a{display:inline-block;white-space:nowrap;text-decoration:none;padding:5px 9px;border:1px solid var(--line);border-radius:999px;background:var(--soft)}.date,.badge{white-space:normal}}
@media (max-width:389px){.contacts{display:grid;grid-template-columns:repeat(2,auto);justify-content:center}}
@media (max-width:420px){.summary .photo{float:none;display:block;width:200px;margin:4px auto 14px}}
@media print{.sidebar,.totop{display:none!important}.cv{padding:0}body{font-size:12px}.item{break-inside:avoid}a::after{content:none!important}}
"""

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%231155cc'/%3E"
    "%3Ctext x='32' y='42' font-family='Arial,sans-serif' font-size='28' font-weight='700' fill='white' text-anchor='middle'%3ERB%3C/text%3E%3C/svg%3E"
)


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def external(html_str: str) -> str:
    """Open off-site links in a new tab."""
    return re.sub(r'<a href="(https?://[^"]+)">', r'<a href="\1" target="_blank" rel="noopener">', html_str)


# Google Docs "single" line spacing is the font's natural line height (~1.2 for Lato/Raleway).
DOC_LINE = 1.2


def doc_css(st: dict | None, text=True) -> str:
    """Inline style reproducing the Doc's per-paragraph spacing."""
    st = st or {}
    out = []
    if st.get("pt"):
        out.append(f"padding-top:{st['pt']:g}em")
    if st.get("pb"):
        out.append(f"padding-bottom:{st['pb']:g}em")
    if st.get("lh"):
        out.append(f"line-height:{round(st['lh'] * DOC_LINE, 3):g}")
    if text and st.get("align") in ("center", "right"):  # body text is justified by default
        out.append(f"text-align:{st['align']}")
    return f' style="{";".join(out)}"' if out else ""


def render_blocks(blocks: list) -> str:
    out = []
    for b in blocks:
        sty = doc_css(b.get("style"), text=b["type"] in ("p", "list"))
        if b["type"] == "p":
            out.append(f"<p{sty}>{external(b['html'])}</p>")
        elif b["type"] == "list":
            out.append(f'<ul class="bullets"{sty}>' + "".join(f"<li>{external(i)}</li>" for i in b["items"]) + "</ul>")
        elif b["type"] == "links":
            out.append(f'<div class="links"{sty}>' + "".join(
                f'<a href="{esc(l["url"])}" target="_blank" rel="noopener">{esc(l["label"])}</a>' for l in b["links"]
            ) + "</div>")
        elif b["type"] == "tech":
            out.append(f'<ul class="tech" aria-label="Tech stack"{sty}>' + "".join(f"<li>{esc(t)}</li>" for t in b["tags"]) + "</ul>")
    return "\n".join(out)


def meta_html(item: dict) -> str:
    meta = item.get("meta")
    if not meta:
        return ""
    if item.get("meta_kind") == "result":
        low = meta.lower()
        cls = "badge win" if re.search(r"winner|place|prize|champion", low) else "badge fin" if "finalist" in low else "badge"
        return f'<span class="{cls}">{esc(meta)}</span>'
    return f'<span class="date">{esc(meta)}</span>'


def plain(html_str: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_str))


FONTS = ROOT / "assets" / "fonts"


def make_og_image(person: dict, host: str, out: Path) -> tuple[int, int]:
    """1200x630 link-preview card (Telegram, Slack, LinkedIn, X...): photo + name + role."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    W, H, PAD, PHOTO_W = 1200, 630, 72, 430
    ink, muted, accent = (29, 29, 31), (95, 99, 104), (17, 85, 204)
    img = Image.new("RGB", (W, H), "white")
    if PHOTO.exists():
        with Image.open(PHOTO) as ph:
            img.paste(ImageOps.fit(ph.convert("RGB"), (PHOTO_W, H), centering=(0.5, 0.35)), (W - PHOTO_W, 0))
    d = ImageDraw.Draw(img)
    font = lambda name, size: ImageFont.truetype(str(FONTS / f"{name}.ttf"), size)
    text_w = W - PHOTO_W - 2 * PAD

    def wrap(text: str, f, sep=" ") -> list[str]:
        lines, cur = [], ""
        for word in text.split(sep):
            cand = f"{cur}{sep}{word}" if cur else word
            if f.getlength(cand) <= text_w or not cur:
                cur = cand
            else:
                lines.append(cur + (sep.rstrip() if sep.strip() else ""))
                cur = word
        return lines + [cur]

    size = 70
    while font("Raleway-bold", size).getlength(person["name"].upper()) > text_w and size > 40:
        size -= 2
    y = PAD + 10
    d.text((PAD, y), person["name"].upper(), font=font("Raleway-bold", size), fill=ink)
    y += size + 22
    d.rounded_rectangle((PAD, y, PAD + 90, y + 8), radius=4, fill=accent)
    y += 40
    f_role = font("Lato-bold", 34)
    for line in wrap(person["job_title"], f_role, sep=" · ")[:3]:
        d.text((PAD, y), line, font=f_role, fill=ink)
        y += 46
    y += 18
    tagline = person.get("og_tagline") or " · ".join(person.get("knows_about", [])[:5])
    f_tag = font("Lato-regular", 27)
    for line in wrap(tagline, f_tag, sep=" · ")[:3]:
        d.text((PAD, y), line, font=f_tag, fill=muted)
        y += 38
    d.text((PAD, H - PAD - 34), host, font=font("Lato-bold", 32), fill=accent)
    img.save(out, "PNG", optimize=True)
    return W, H


def cmd_render() -> None:
    cfg = load_config()
    person, url = cfg["person"], cfg["url"].rstrip("/")
    content = json.loads(CV_JSON.read_text(encoding="utf-8"))
    sections = content["sections"]
    summary = next((s for s in sections if s.get("is_summary")), None)
    body_sections = [s for s in sections if s is not summary]
    updated = content.get("updated") or dt.date.today().isoformat()

    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()
    has_photo = PHOTO.exists()
    if has_photo:
        shutil.copy(PHOTO, SITE / "photo.jpg")
        from PIL import Image

        with Image.open(PHOTO) as im:
            pw, ph = im.size

    title = f"{person['name']} — {person['job_title']}"
    og_w, og_h = make_og_image(person, urlparse(url).hostname or url, SITE / "og.png")
    verify_names = {"google": "google-site-verification", "yandex": "yandex-verification", "bing": "msvalidate.01"}
    verification = "".join(
        f'<meta name="{verify_names.get(k, k)}" content="{esc(v)}">\n'
        for k, v in cfg.get("verification", {}).items() if v
    )
    description = person.get("description") or (plain(summary["intro"][0]["html"])[:155] if summary else title)
    contacts = cfg.get("contacts", [])
    same_as = [c["url"] for c in contacts if c["url"].startswith("http")]

    person_ld = {
        "@type": "Person",
        "@id": f"{url}/#person",
        "name": person["name"],
        "alternateName": person.get("alternate_names", []),
        "jobTitle": person["job_title"],
        "description": description,
        "url": f"{url}/",
        "sameAs": same_as,
    }
    if has_photo:
        person_ld["image"] = f"{url}/photo.jpg"
    if person.get("email"):
        person_ld["email"] = f"mailto:{person['email']}"
    if person.get("alumni_of"):
        person_ld["alumniOf"] = [{"@type": "CollegeOrUniversity", "name": n} for n in person["alumni_of"]]
    if person.get("knows_about"):
        person_ld["knowsAbout"] = person["knows_about"]
    json_ld = {
        "@context": "https://schema.org",
        "@type": "ProfilePage",
        "url": f"{url}/",
        "name": title,
        "dateModified": updated,
        "inLanguage": cfg.get("lang", "en"),
        "mainEntity": person_ld,
    }

    contacts_html = "".join(
        f'<li><a href="{esc(c["url"])}"'
        + ("" if c["url"].startswith("mailto:") else ' target="_blank" rel="noopener me"')
        + f'>{esc(c.get("icon", ""))} <span class="full">{esc(c["label"])}</span>'
        + (f'<span class="short">{esc(c["short"])}</span>' if c.get("short") else "")
        + "</a></li>"
        for c in contacts
    )
    nav_html = "".join(
        f'<li><a href="#{esc(s["id"])}">{esc(s["title"])}</a></li>'
        for s in ([summary] if summary else []) + body_sections
    )
    sections_html = []
    for s in body_sections:
        items = [render_blocks(s["intro"])] if s["intro"] else []
        for it in s["items"]:
            items.append(
                f'<article class="item"><header><h3{doc_css(it.get("style"), text=False)}>{external(it["title_html"])}</h3>'
                f'{meta_html(it)}</header>\n'
                f'{render_blocks(it["blocks"])}\n</article>'
            )
        sections_html.append(
            f'<section id="{esc(s["id"])}" aria-labelledby="{esc(s["id"])}-h">'
            f'<h2 id="{esc(s["id"])}-h"{doc_css(s.get("style"), text=False)}>{esc(s["title"])}</h2>\n' + "\n".join(items) + "</section>"
        )

    og_image = (
        f'<meta property="og:image" content="{url}/og.png">\n'
        f'<meta property="og:image:type" content="image/png">\n'
        f'<meta property="og:image:width" content="{og_w}">\n<meta property="og:image:height" content="{og_h}">\n'
        f'<meta property="og:image:alt" content="{esc(title)}">\n'
        f'<meta name="twitter:image" content="{url}/og.png">\n'
    )
    photo_tag = (
        f'<img class="photo" src="photo.jpg" width="{pw}" height="{ph}" alt="{esc(person["name"])}" fetchpriority="high">'
        if has_photo else ""
    )
    # keep "Full-stack" etc. in one piece; wrap only between the " · " parts
    role_html = " · ".join(f'<span class="nw">{esc(part)}</span>' for part in person["job_title"].split(" · "))
    updated_human = dt.date.fromisoformat(updated).strftime("%B %Y")
    alt_names = " · ".join(person.get("alternate_names", [])[:1])

    page = f"""<!DOCTYPE html>
<html lang="{esc(cfg.get('lang', 'en'))}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<meta name="author" content="{esc(person['name'])}">
{verification}<link rel="canonical" href="{url}/">
<meta name="theme-color" content="#1155cc">
<link rel="icon" href="{FAVICON}">
<meta property="og:type" content="profile">
<meta property="og:url" content="{url}/">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:site_name" content="{esc(person['name'])}">
<meta property="profile:first_name" content="{esc(person['name'].split()[0])}">
<meta property="profile:last_name" content="{esc(person['name'].split()[-1])}">
<meta property="og:locale" content="en_US">
{og_image}<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(description)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;1,400&family=Raleway:wght@700;800&display=swap" rel="stylesheet">
<style>{CSS}</style>
<script type="application/ld+json">{json.dumps(json_ld, ensure_ascii=False)}</script>
</head>
<body>
<div class="layout">
<aside class="sidebar"><nav aria-label="Sections"><ul>{nav_html}</ul></nav></aside>
<main class="cv">
<header class="hero">
<h1>{esc(person['name'])}</h1>
<p class="role">{role_html}</p>
<ul class="contacts">{contacts_html}</ul>
</header>
<section id="{esc(summary['id'] if summary else 'summary')}" class="summary" aria-labelledby="summary-h">
<h2 id="summary-h"{doc_css(summary.get("style") if summary else None, text=False)}>{esc(summary['title'] if summary else 'Summary')}</h2>
{photo_tag}
{render_blocks(summary['intro']) if summary else ''}
</section>
{chr(10).join(sections_html)}
<footer><span>© {dt.date.today().year} {esc(person['name'])}{' · ' + esc(alt_names) if alt_names else ''}</span>
<span>Updated <time datetime="{updated}">{updated_human}</time></span></footer>
</main>
</div>
<button class="totop" type="button" aria-label="Back to top"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>
<script>
(function(){{
  var body=document.body, nav=document.querySelector('.sidebar nav'), aside=document.querySelector('.sidebar'),
      ul=nav.querySelector('ul'), hero=document.querySelector('.hero'), head=document.getElementById('summary-h');
  var links=[].slice.call(nav.querySelectorAll('a'));
  var secs=links.map(function(a){{return document.getElementById(a.getAttribute('href').slice(1));}});
  var mobile=window.matchMedia('(max-width:959px)'), current=-1;
  function align(){{  /* desktop: put the first sidebar item level with the Summary heading */
    if(mobile.matches||!head||!links.length) return;
    var h=head.getBoundingClientRect(), top=aside.getBoundingClientRect().top+window.scrollY;
    var want=h.top+window.scrollY+h.height/2-(top+links[0].offsetTop+links[0].offsetHeight/2);
    nav.style.setProperty('--nav-offset',Math.max(0,Math.round(want))+'px');
  }}
  function update(){{
    var y=window.scrollY+window.innerHeight*0.3, i=0;
    secs.forEach(function(s,k){{ if(s && s.offsetTop<=y) i=k; }});
    if(window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-2) i=secs.length-1;
    if(i!==current){{
      current=i;
      links.forEach(function(a,k){{ a.classList.toggle('active',k===i); }});
      if(mobile.matches){{ var a=links[i]; ul.scrollTo({{left:a.offsetLeft-ul.clientWidth/2+a.offsetWidth/2,behavior:'smooth'}}); }}
    }}
    body.classList.toggle('nav-on',mobile.matches && window.scrollY>hero.offsetTop+hero.offsetHeight);
    body.classList.toggle('totop-on',mobile.matches && window.scrollY>window.innerHeight*1.2);
  }}
  document.querySelector('.totop').addEventListener('click',function(){{ window.scrollTo({{top:0,behavior:'smooth'}}); }});
  window.addEventListener('scroll',update,{{passive:true}});
  window.addEventListener('resize',function(){{ align(); current=-1; update(); }});
  window.addEventListener('load',align);
  if(document.fonts&&document.fonts.ready) document.fonts.ready.then(align);
  align(); update();
}})();
</script>
</body>
</html>
"""
    (SITE / "index.html").write_text(page, encoding="utf-8")
    (SITE / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {url}/sitemap.xml\n", encoding="utf-8")
    (SITE / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{url}/</loc><lastmod>{updated}</lastmod></url>\n</urlset>\n",
        encoding="utf-8",
    )
    host = urlparse(url).hostname
    if host and not host.endswith("github.io"):
        (SITE / "CNAME").write_text(host + "\n", encoding="utf-8")
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Built {SITE / 'index.html'} ({len(page.encode())} bytes, {len(body_sections)} sections)")


def cmd_build(_args) -> None:
    cmd_parse()
    cmd_render()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch").set_defaults(fn=cmd_fetch)
    sub.add_parser("build").set_defaults(fn=cmd_build)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
