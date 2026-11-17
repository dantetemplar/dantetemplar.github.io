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
    print(f"Fetched Google Doc {doc_id}: {len(data)} bytes")


# --------------------------------------------------------------------------- parse

def parse_docs_export(html_path: Path) -> tuple[dict, Path | None]:
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    css = soup.style.string if soup.style and soup.style.string else ""
    bold_cls = set(re.findall(r"\.(c\d+)\{[^}]*font-weight:700", css))
    ital_cls = set(re.findall(r"\.(c\d+)\{[^}]*font-style:italic", css))

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
        if links and not rest.strip():
            target.append({"type": "links", "links": [
                {"label": tidy(a.get_text()), "url": real_url(a["href"])} for a in links if tidy(a.get_text())
            ]})
        elif as_list_item:
            if target and target[-1]["type"] == "list":
                target[-1]["items"].append(tidy(inline(p)))
            else:
                target.append({"type": "list", "items": [tidy(inline(p))]})
        elif not tech_m:
            target.append({"type": "p", "html": tidy(inline(p))})
        if tech_m:
            tags = [t.strip() for t in re.split(r"[,;]", tech_m.group(1)) if t.strip()]
            target.append({"type": "tech", "tags": tags})

    def add_heading(h):
        nonlocal item
        a = h.find("a")
        t = text_of(h)
        # A heading that is only a link, right after an item with no body = that item's link.
        if a and t == tidy(a.get_text()) and item and not item["blocks"] and item.get("meta"):
            item["blocks"].append({"type": "links", "links": [{"label": t, "url": real_url(a["href"])}]})
            return
        title_html, meta, kind = tidy(inline(h, allow_bold=False)), None, None
        if " | " in t:
            title, meta = (s.strip() for s in t.rsplit(" | ", 1))
            title_html, kind = html.escape(title, quote=False), "date"
        else:
            m = re.match(r"^(.*?)\s*[—–]\s*((?:19|20)\d\d\b.*)$", t)
            if m:
                title_html, meta, kind = html.escape(m.group(1).strip(), quote=False), m.group(2).strip(), "result"
        item = {"title_html": title_html, "meta": meta, "meta_kind": kind, "blocks": []}
        sec["items"].append(item)

    for el in soup.body.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name == "h1" and text_of(el):
            sec = {"title": text_of(el), "intro": [], "items": []}
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
    return {"sections": sections}, (photo if photo and photo.is_file() else None)


def cmd_parse() -> None:
    htmls = sorted(SOURCE.glob("*.html")) if SOURCE.exists() else []
    if not htmls:
        print("No source/*.html - keeping committed content/.")
        return
    content, photo_src = parse_docs_export(htmls[0])
    CONTENT.mkdir(exist_ok=True)

    old = json.loads(CV_JSON.read_text(encoding="utf-8")) if CV_JSON.exists() else {}
    if photo_src:
        digest = hashlib.sha256(photo_src.read_bytes()).hexdigest()
        content["photo_sha256"] = digest
        if old.get("photo_sha256") != digest or not PHOTO.exists():
            from PIL import Image, ImageOps

            im = ImageOps.exif_transpose(Image.open(photo_src)).convert("RGB")
            im.thumbnail((600, 800))
            im.save(PHOTO, "JPEG", quality=85, optimize=True, progressive=True)
    elif "photo_sha256" in old:
        content["photo_sha256"] = old["photo_sha256"]

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
--accent:#d9480f;--accent-soft:#fdeee8;--win:#137333;--win-soft:#e6f4ea;--fin:#8a5a00;--fin-soft:#fef3d8;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--ink:#f2f2f4;--body:#d6d6da;--muted:#a1a1aa;--line:#2a2a2e;
--soft:#1c1c20;--accent:#ff8a5c;--accent-soft:#3a2219;--win:#7fd99a;--win-soft:#16301f;--fin:#f0c76b;--fin-soft:#33290f;color-scheme:dark}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.65 "Lato",system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.cv{max-width:880px;margin:0 auto;padding:40px 20px 56px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.hero{display:grid;grid-template-columns:190px 1fr;gap:32px;align-items:start}
.hero img{width:190px;height:253px;object-fit:cover;border-radius:16px;display:block;background:var(--soft)}
h1{font:800 38px/1.1 "Raleway",system-ui,sans-serif;margin:2px 0 8px;letter-spacing:-.01em}
.role{font:700 18px/1.35 "Raleway",system-ui,sans-serif;color:var(--accent);margin:0 0 16px}
.contacts{list-style:none;display:flex;flex-wrap:wrap;gap:8px;padding:0;margin:0 0 18px}
.contacts a{display:inline-block;color:var(--ink);background:var(--soft);border:1px solid var(--line);padding:6px 12px;border-radius:999px;font-size:14px}
.contacts a:hover{border-color:var(--accent);color:var(--accent);text-decoration:none}
.summary p{margin:0 0 10px;color:var(--body)}
nav{position:sticky;top:0;z-index:1;background:var(--bg);display:flex;flex-wrap:wrap;gap:8px;margin:26px 0 4px;padding:12px 0;
border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
nav a{font:700 13px/1 "Raleway",system-ui,sans-serif;color:var(--ink);padding:8px 12px;border-radius:999px;background:var(--soft)}
nav a:hover{background:var(--accent-soft);color:var(--accent);text-decoration:none}
section{padding-top:24px;scroll-margin-top:64px}
h2{font:800 23px/1.2 "Raleway",system-ui,sans-serif;margin:0 0 6px;display:flex;align-items:center;gap:10px}
h2::before{content:"";width:6px;height:23px;border-radius:3px;background:var(--accent)}
.item{padding:16px 0;border-bottom:1px solid var(--line)}
.item:last-child{border-bottom:0}
.item header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:4px 14px;margin-bottom:6px}
h3{font:700 17px/1.35 "Raleway",system-ui,sans-serif;margin:0;flex:1 1 380px}
.date{color:var(--muted);font-size:14px;white-space:nowrap}
.badge{font:700 12.5px/1 "Lato",sans-serif;padding:6px 10px;border-radius:999px;background:var(--soft);color:var(--muted);white-space:nowrap}
.badge.win{background:var(--win-soft);color:var(--win)}
.badge.fin{background:var(--fin-soft);color:var(--fin)}
.item p{margin:0 0 8px;color:var(--body)}
.item ul.bullets{margin:0 0 8px;padding-left:20px;color:var(--body)}
.links{display:flex;flex-wrap:wrap;gap:6px 16px;margin:2px 0 8px;font-size:15px}
.links a::after{content:" ↗";font-size:.85em}
.tech{list-style:none;display:flex;flex-wrap:wrap;gap:6px;padding:0;margin:4px 0 6px}
.tech li{font-size:12.5px;line-height:1;padding:6px 9px;border-radius:6px;background:var(--soft);color:var(--muted);border:1px solid var(--line)}
footer{margin-top:36px;padding-top:16px;border-top:1px solid var(--line);color:var(--muted);font-size:13.5px;display:flex;flex-wrap:wrap;gap:6px 18px;justify-content:space-between}
@media (max-width:640px){.cv{padding:24px 16px 40px}.hero{grid-template-columns:1fr;gap:18px}
.hero img{width:140px;height:187px}h1{font-size:30px}.date,.badge{white-space:normal}nav{margin-top:20px}}
@media print{nav{display:none}.cv{padding:0}body{font-size:12px}.item{break-inside:avoid}a::after{content:none!important}}
"""

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23d9480f'/%3E"
    "%3Ctext x='32' y='42' font-family='Arial,sans-serif' font-size='28' font-weight='700' fill='white' text-anchor='middle'%3ERB%3C/text%3E%3C/svg%3E"
)


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def external(html_str: str) -> str:
    """Open off-site links in a new tab."""
    return re.sub(r'<a href="(https?://[^"]+)">', r'<a href="\1" target="_blank" rel="noopener">', html_str)


def render_blocks(blocks: list) -> str:
    out = []
    for b in blocks:
        if b["type"] == "p":
            out.append(f"<p>{external(b['html'])}</p>")
        elif b["type"] == "list":
            out.append('<ul class="bullets">' + "".join(f"<li>{external(i)}</li>" for i in b["items"]) + "</ul>")
        elif b["type"] == "links":
            out.append('<div class="links">' + "".join(
                f'<a href="{esc(l["url"])}" target="_blank" rel="noopener">{esc(l["label"])}</a>' for l in b["links"]
            ) + "</div>")
        elif b["type"] == "tech":
            out.append('<ul class="tech" aria-label="Tech stack">' + "".join(f"<li>{esc(t)}</li>" for t in b["tags"]) + "</ul>")
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
        + f'>{esc(c.get("icon", ""))} {esc(c["label"])}</a></li>'
        for c in contacts
    )
    nav_html = "".join(f'<a href="#{esc(s["id"])}">{esc(s["title"])}</a>' for s in body_sections)
    sections_html = []
    for s in body_sections:
        items = [render_blocks(s["intro"])] if s["intro"] else []
        for it in s["items"]:
            items.append(
                f'<article class="item"><header><h3>{external(it["title_html"])}</h3>{meta_html(it)}</header>\n'
                f'{render_blocks(it["blocks"])}\n</article>'
            )
        sections_html.append(
            f'<section id="{esc(s["id"])}" aria-labelledby="{esc(s["id"])}-h">'
            f'<h2 id="{esc(s["id"])}-h">{esc(s["title"])}</h2>\n' + "\n".join(items) + "</section>"
        )

    og_image = (
        f'<meta property="og:image" content="{url}/photo.jpg">\n'
        f'<meta property="og:image:width" content="{pw}">\n<meta property="og:image:height" content="{ph}">\n'
        f'<meta property="og:image:alt" content="{esc(person["name"])}">\n'
        if has_photo else ""
    )
    photo_tag = (
        f'<img src="photo.jpg" width="{pw}" height="{ph}" alt="{esc(person["name"])}" fetchpriority="high">'
        if has_photo else ""
    )
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
<link rel="canonical" href="{url}/">
<meta name="theme-color" content="#d9480f">
<link rel="icon" href="{FAVICON}">
<meta property="og:type" content="profile">
<meta property="og:url" content="{url}/">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:site_name" content="{esc(person['name'])}">
<meta property="profile:first_name" content="{esc(person['name'].split()[0])}">
<meta property="profile:last_name" content="{esc(person['name'].split()[-1])}">
{og_image}<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(description)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Lato:ital,wght@0,400;0,700;1,400&family=Raleway:wght@700;800&display=swap" rel="stylesheet">
<style>{CSS}</style>
<script type="application/ld+json">{json.dumps(json_ld, ensure_ascii=False)}</script>
</head>
<body>
<main class="cv">
<header class="hero">
{photo_tag}
<div>
<h1>{esc(person['name'])}</h1>
<p class="role">{esc(person['job_title'])}</p>
<ul class="contacts">{contacts_html}</ul>
<div class="summary">
{render_blocks(summary['intro']) if summary else ''}
</div>
</div>
</header>
<nav aria-label="Sections">{nav_html}</nav>
{chr(10).join(sections_html)}
<footer><span>© {dt.date.today().year} {esc(person['name'])}{' · ' + esc(alt_names) if alt_names else ''}</span>
<span>Updated <time datetime="{updated}">{updated_human}</time></span></footer>
</main>
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
