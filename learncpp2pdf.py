#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
learncpp2pdf.py - build a single hyperlinked PDF from learncpp.com.

Produces one PDF with a clickable table of contents (including page numbers),
working cross-references between lessons, PDF bookmarks, syntax-highlighted
code, expanded "Show Solution" blocks, and no comment section or ads.

Runs from the command line or from a notebook cell via run(). Cache and output
locations are configurable, so on Colab you can point them at Google Drive and
survive a runtime disconnect without re-downloading every page.

Personal use only. The learncpp.com FAQ permits converting pages to PDF for
yourself but not redistributing the result.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

try:
    from tqdm.auto import tqdm
except ImportError:                                    # tqdm is optional
    def tqdm(x=None, **k):
        return x if x is not None else None

# ----------------------------------------------------------------------------
# Paths (override with configure())
# ----------------------------------------------------------------------------

BASE = "https://www.learncpp.com/"
UA = "Mozilla/5.0 (compatible; personal-offline-archiver/1.0)"

CACHE = Path("cache")
PAGES = CACHE / "pages"
IMGS = CACHE / "img"
WORK = Path(".")


def configure(cache_dir="cache", work_dir="."):
    """Set where the page/image cache and the intermediate book.html live."""
    global CACHE, PAGES, IMGS, WORK
    CACHE = Path(cache_dir)
    PAGES = CACHE / "pages"
    IMGS = CACHE / "img"
    WORK = Path(work_dir)
    for d in (CACHE, PAGES, IMGS, WORK):
        d.mkdir(parents=True, exist_ok=True)
    return CACHE


LESSON_RE = re.compile(r"^https?://(?:www\.)?learncpp\.com/cpp-tutorial/([^/?#]+)/?$", re.I)
CHAPTER_RE = re.compile(r"^\s*(Chapter\s+[0-9A-Za-z]+|Appendix\s+[0-9A-Za-z]+)\s*$")
NUM_RE = re.compile(r"^\s*([0-9]+|[A-Z])\.(?:[0-9]+|[a-zA-Z])\s*$")

DROP_SELECTORS = [
    "script", "style", "noscript", "iframe", "ins", "form", "svg",
    "#comments", "#wpdcom", "#respond", ".comments-area", ".wpd-form",
    "#masthead", "#colophon", ".site-header", ".site-footer",
    ".prevnext", ".prevnext-inline", ".post-navigation", ".nav-links",
    ".entry-meta", ".author-info", ".sharedaddy", ".jp-relatedposts",
    ".adsbygoogle", ".ezoic-ad", ".ezoic-adpicker-ad", ".ezoic-videopicker-video",
    "[id^='ez-']", "[class*='ezoic']", "[id*='google_ads']", "[class*='adsense']",
    ".solution_link_show", ".solution_link_hide", ".hidden_solution_link",
    ".wpdiscuz-subscribe-bar",
]

# ----------------------------------------------------------------------------
# Networking + cache (thread-safe)
# ----------------------------------------------------------------------------

class Fetcher:
    def __init__(self, delay=0.4, refresh=False, timeout=30):
        self.delay = delay
        self.refresh = refresh
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self):
        with self._lock:                       # one global rate limit across threads
            dt = time.time() - self._last
            if dt < self.delay:
                time.sleep(self.delay - dt)
            self._last = time.time()

    def cache_path(self, url: str) -> Path:
        return PAGES / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".html")

    def cached(self, url: str) -> bool:
        return self.cache_path(url).exists() and not self.refresh

    def get_text(self, url: str) -> str:
        PAGES.mkdir(parents=True, exist_ok=True)
        f = self.cache_path(url)
        if f.exists() and not self.refresh:
            return f.read_text("utf-8", errors="replace")
        last = None
        for attempt in range(4):
            try:
                self._throttle()
                r = self.s.get(url, timeout=self.timeout)
                r.raise_for_status()
                r.encoding = r.encoding or "utf-8"
                f.write_text(r.text, "utf-8")
                return r.text
            except Exception as e:
                last = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{url}: {last}")

    def get_bytes(self, url: str):
        try:
            self._throttle()
            r = self.s.get(url, timeout=self.timeout)
            r.raise_for_status()
            return r.content
        except Exception:
            return None

    def prefetch(self, urls, workers=4):
        """Fill the cache in parallel. Hosted notebooks time out, so the less
        wall-clock time spent on network I/O the better."""
        todo = [u for u in urls if not self.cached(u)]
        if not todo:
            print(f"  all {len(urls)} pages already cached")
            return []
        errs = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.get_text, u): u for u in todo}
            bar = tqdm(total=len(todo), desc="downloading", unit="page")
            for fu in as_completed(futs):
                try:
                    fu.result()
                except Exception as e:
                    errs.append((futs[fu], e))
                if bar is not None:
                    bar.update(1)
            if bar is not None:
                bar.close()
        for u, e in errs:
            print(f"  download failed: {u} ({e})", file=sys.stderr)
        return errs


# ----------------------------------------------------------------------------
# 1. Table of contents
# ----------------------------------------------------------------------------

def collect_toc(html: str, limit: int | None = None):
    soup = BeautifulSoup(html, "lxml")

    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        if "latest change" in h.get_text(" ", strip=True).lower():
            for sib in list(h.find_all_next()):
                sib.extract()
            h.extract()
            break

    items, seen = [], set()
    chapter = chapter_title_pending = pending_num = None

    for node in soup.descendants:
        if isinstance(node, NavigableString):
            t = str(node).strip()
            if not t:
                continue
            m = CHAPTER_RE.match(t)
            if m:
                chapter = m.group(1)
                chapter_title_pending = chapter
                pending_num = None
                continue
            if chapter_title_pending == chapter and chapter and t != chapter:
                items.append({"type": "chapter", "num": chapter, "title": t})
                chapter_title_pending = None
                continue
            if NUM_RE.match(t):
                pending_num = t
            continue

        if isinstance(node, Tag) and node.name == "a":
            href = urldefrag(urljoin(BASE, node.get("href", "")))[0]
            m = LESSON_RE.match(href)
            if not m:
                continue
            slug = m.group(1)
            if slug in seen:
                continue
            seen.add(slug)
            items.append({"type": "lesson", "num": pending_num or "",
                          "title": node.get_text(" ", strip=True),
                          "url": href, "slug": slug})
            pending_num = None
            if limit and sum(1 for i in items if i["type"] == "lesson") >= limit:
                break

    out = []
    for i, it in enumerate(items):
        if it["type"] == "chapter" and not any(
                x["type"] == "lesson" for x in items[i + 1:i + 60]):
            continue
        out.append(it)
    return out


# ----------------------------------------------------------------------------
# 2. Lesson extraction and cleanup
# ----------------------------------------------------------------------------

def _drop(soup):
    for sel in DROP_SELECTORS:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            pass


def _unhide_solutions(root: Tag):
    for el in root.select(".wpsolution, .solution, [class*='solution']"):
        st = el.get("style", "")
        if st:
            el["style"] = re.sub(r"display\s*:\s*none\s*;?", "", st, flags=re.I)
        cls = el.get("class", [])
        if "wpsolution" in cls:
            el["class"] = cls + ["pdf-solution"]
    for el in root.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        el["style"] = re.sub(r"display\s*:\s*none\s*;?", "",
                             el.get("style", ""), flags=re.I)


def relocate_solutions(body: Tag, slug: str, sink: list, lesson_title: str):
    """Move each solution block out of the lesson body into an appendix.

    In its place we leave a link; the appendix entry links back. This is the
    only "hidden until you ask for it" mechanism that works in every PDF
    viewer -- see the README for why layers and JavaScript do not.
    """
    blocks = body.select(".pdf-solution, .wpsolution")
    if not blocks:
        return 0
    a = anchor_for(slug)
    sink.append(f"<h3 class='sol-lesson' id='{a}--solutions'>{esc(lesson_title)}</h3>")
    for n, block in enumerate(blocks, 1):
        qid, sid = f"{a}--q{n}", f"{a}--s{n}"
        link = BeautifulSoup(
            f"<p class='solution-link' id='{qid}'>"
            f"<a href='#{sid}'>Show solution {n} &rarr;</a></p>", "lxml").p
        block.insert_before(link)
        block.extract()
        block["id"] = sid
        cls = [c for c in block.get("class", []) if c != "wpsolution"]
        block["class"] = cls + ["pdf-solution", "in-appendix"]
        back = BeautifulSoup(
            f"<p class='solution-back'><a href='#{qid}'>&larr; back to the question</a></p>",
            "lxml").p
        block.append(back)
        sink.append(str(block))
    return len(blocks)


def _highlight_code(root: Tag, enable=True):
    if not enable:
        return
    try:
        from pygments import highlight
        from pygments.lexers import get_lexer_by_name, CppLexer
        from pygments.formatters import HtmlFormatter
    except ImportError:
        return
    fmt = HtmlFormatter(nowrap=True, style="friendly")
    for pre in root.find_all("pre"):
        cls = " ".join(pre.get("class", []))
        m = re.search(r"brush\s*:\s*([a-z+#-]+)", cls, re.I)
        lang = (m.group(1) if m else "cpp").lower()
        code = pre.get_text()
        if not code.strip():
            continue
        try:
            lexer = get_lexer_by_name({"c++": "cpp", "plain": "text"}.get(lang, lang))
        except Exception:
            lexer = CppLexer()
        try:
            html = highlight(code, lexer, fmt)
        except Exception:
            continue
        pre.replace_with(BeautifulSoup(f"<pre class='code'>{html}</pre>", "lxml").pre)


def extract_lesson(html: str, url: str):
    soup = BeautifulSoup(html, "lxml")
    _drop(soup)
    title_el = soup.select_one("h1.entry-title, h1.post-title, article h1, h1")
    title = title_el.get_text(" ", strip=True) if title_el else url
    body = (soup.select_one("div.entry-content")
            or soup.select_one("article .post-content")
            or soup.select_one("article")
            or soup.select_one("#content")
            or soup.body)
    if body is None:
        return title, None
    if title_el is not None and title_el in body.descendants:
        title_el.extract()
    for img in body.find_all("img"):
        src = img.get("src", "")
        if "stripe" in src or "learncpp.png" in src:
            img.decompose()
    _unhide_solutions(body)
    return title, body


# ----------------------------------------------------------------------------
# 3. Link rewriting
# ----------------------------------------------------------------------------

def anchor_for(slug: str) -> str:
    return "L-" + re.sub(r"[^A-Za-z0-9_-]", "-", slug)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", s)


def rewrite(body: Tag, slug: str, slug_set: set):
    a = anchor_for(slug)
    idmap = {}
    for el in body.find_all(id=True):
        old = el["id"]
        idmap[old] = f"{a}--{_safe(old)}"
        el["id"] = idmap[old]
    for el in body.find_all(attrs={"name": True}):
        if el.name == "a":
            old = el["name"]
            idmap.setdefault(old, f"{a}--{_safe(old)}")
            el["name"] = idmap[old]

    for link in body.find_all("a", href=True):
        href = link["href"].strip()
        if href.startswith("#"):
            frag = href[1:]
            link["href"] = "#" + idmap.get(frag, f"{a}--{_safe(frag)}")
            continue
        absu = urljoin(BASE, href)
        base_u, frag = urldefrag(absu)
        m = LESSON_RE.match(base_u)
        if m and m.group(1) in slug_set:
            ta = anchor_for(m.group(1))
            link["href"] = f"#{ta}--{_safe(frag)}" if frag else f"#{ta}"
            link["class"] = link.get("class", []) + ["xref"]
            continue
        host = urlparse(base_u).netloc.lower().replace("www.", "")
        if host == "learncpp.com" and urlparse(base_u).path.strip("/") == "":
            link["href"] = "#toc"
            continue
        link["href"] = absu
        link["class"] = link.get("class", []) + ["ext"]


# ----------------------------------------------------------------------------
# 4. Images
# ----------------------------------------------------------------------------

def localize_images(body: Tag, fetcher: Fetcher, enable=True, max_w=900, quality=78):
    if not enable:
        for img in body.find_all("img"):
            img.decompose()
        return
    IMGS.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
    except ImportError:
        Image = None
    for img in body.find_all("img"):
        src = img.get("data-src") or img.get("src") or ""
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        absu = urljoin(BASE, src)
        key = hashlib.sha1(absu.encode()).hexdigest()[:16]
        existing = list(IMGS.glob(key + ".*"))
        if existing:
            img["src"] = str(existing[0].resolve().as_uri())
        else:
            data = fetcher.get_bytes(absu)
            if not data:
                img.decompose()
                continue
            ext = os.path.splitext(urlparse(absu).path)[1].lower() or ".png"
            out = IMGS / (key + ext)
            if Image is not None and ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                try:
                    im = Image.open(io.BytesIO(data))
                    if getattr(im, "is_animated", False):
                        im.seek(0)
                    if im.width > max_w:
                        im = im.resize((max_w, round(im.height * max_w / im.width)),
                                       Image.LANCZOS)
                    if im.mode in ("P", "LA", "RGBA"):
                        out = IMGS / (key + ".png")
                        im.save(out, "PNG", optimize=True)
                    else:
                        out = IMGS / (key + ".jpg")
                        im.convert("RGB").save(out, "JPEG", quality=quality,
                                               optimize=True, progressive=True)
                except Exception:
                    out.write_bytes(data)
            else:
                out.write_bytes(data)
            img["src"] = str(out.resolve().as_uri())
        for attr in ("srcset", "data-srcset", "sizes", "loading", "width", "height"):
            img.attrs.pop(attr, None)


# ----------------------------------------------------------------------------
# 5. book.html
# ----------------------------------------------------------------------------

CSS = r"""
@page {
  size: A4;
  margin: 18mm 16mm 18mm 16mm;
  @bottom-center { content: counter(page); font: 9pt "DejaVu Serif", serif; color:#666; }
  @top-center    { content: string(chaptitle); font: 8.5pt "DejaVu Serif", serif; color:#888; }
}
@page :first { @top-center { content: none } @bottom-center { content: none } }

html { font-size: 10.5pt; }
body {
  font-family: "DejaVu Serif", "Liberation Serif", serif;
  line-height: 1.42; color: #1a1a1a; text-align: justify; hyphens: auto;
}
h1, h2, h3, h4 { font-family: "DejaVu Sans", "Liberation Sans", sans-serif;
                 text-align: left; hyphens: none; line-height: 1.2; }

.cover { text-align:center; padding-top: 55mm; page-break-after: always; }
.cover h1 { font-size: 30pt; margin: 0 0 6mm; bookmark-level: none; }
.cover .sub { font-size: 12pt; color:#555; }
.cover .note { margin-top: 40mm; font-size: 9pt; color:#777; }

#toc { page-break-after: always; }
#toc h1 { font-size: 20pt; margin-bottom: 6mm; bookmark-level: 1; }
#toc .toc-chap { font-weight: bold; margin: 4mm 0 1.5mm; font-size: 11pt;
                 font-family: "DejaVu Sans", sans-serif; }
#toc a { text-decoration: none; color:#111; display:block; }
#toc .toc-lesson { margin-left: 6mm; font-size: 9.5pt; }
#toc .toc-lesson a::after {
  content: leader('.') target-counter(attr(href), page); color:#777;
}
#toc .num { display:inline-block; min-width: 12mm; color:#555; }

.chapter-sep { page-break-before: always; padding-top: 30mm; text-align:center; }
.chapter-sep .kicker { font-size: 12pt; color:#777; letter-spacing:.15em;
                       text-transform: uppercase; font-family:"DejaVu Sans",sans-serif; }
.chapter-sep h1 { font-size: 24pt; margin-top: 3mm; text-align:center;
                  string-set: chaptitle content(); bookmark-level: 1; }

section.lesson { page-break-before: always; }
section.lesson > h2 { font-size: 15pt; margin: 0 0 4mm; padding-bottom: 2mm;
                      border-bottom: 1.5px solid #444; bookmark-level: 2; }
section.lesson h3 { font-size: 12pt; margin: 5mm 0 2mm; bookmark-level: 3; }
section.lesson h4 { font-size: 11pt; margin: 4mm 0 2mm; bookmark-level: none; }
section.lesson h5, section.lesson h6 { bookmark-level: none; }
.lesson-num { color:#777; font-weight: normal; }

p { margin: 0 0 2.2mm; orphans: 2; widows: 2; }
ul, ol { margin: 0 0 2.5mm 0; padding-left: 6mm; }
li { margin-bottom: 1mm; }
img { max-width: 100%; height: auto; }

a { color: #0a58a8; text-decoration: none; }
a.ext { color: #7a3ea8; }

pre.code, pre {
  font-family: "DejaVu Sans Mono", "Liberation Mono", monospace;
  font-size: 8.4pt; line-height: 1.32;
  background: #f6f7f9; border: 0.5pt solid #dfe3e8; border-left: 2pt solid #9aa7b4;
  border-radius: 2pt; padding: 2mm 2.5mm; margin: 2.5mm 0;
  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;
  text-align: left; hyphens: none; page-break-inside: avoid;
}
pre.long { page-break-inside: auto; }
code, tt, kbd, samp { font-family: "DejaVu Sans Mono", monospace; font-size: 0.88em;
  background:#f2f3f5; padding: 0 0.6mm; border-radius: 2pt; hyphens: none; }
pre code { background: none; padding: 0; font-size: 1em; }

.cpp-note, .cpp-tip, .cpp-warning, .cpp-rule, .cpp-bestpractice,
.cpp-keyinsight, .cpp-related, .cpp-author, .cpp-nomenclature,
.cpp-asaside, .cpp-optional, .cpp-warningnotice {
  border-left: 2.5pt solid #b9c2cc; background: #f7f9fb;
  padding: 2mm 3mm; margin: 3mm 0; page-break-inside: avoid; font-size: 9.8pt;
}
.cpp-bestpractice { border-color:#3a9a5c; background:#f2faf5; }
.cpp-warning, .cpp-warningnotice { border-color:#c4553a; background:#fdf5f3; }
.cpp-rule, .cpp-keyinsight { border-color:#3a6fa8; background:#f3f7fc; }
.cpp-tip { border-color:#c9a227; background:#fdfaf0; }

.pdf-solution { display: block !important; border: 0.5pt dashed #9aa7b4;
  background:#fbfbfc; padding: 2mm 3mm; margin: 2.5mm 0; }
.pdf-solution::before { content: "Solution"; display:block; font-size: 8.5pt;
  color:#7a8794; font-family:"DejaVu Sans",sans-serif; margin-bottom: 1.5mm; }

/* solutions moved to the appendix */
.solution-link { margin: 2mm 0 3mm; font-family:"DejaVu Sans",sans-serif; font-size: 9.5pt; }
.solution-link a { color:#0a58a8; border: 0.5pt solid #c3ccd6; background:#f4f7fa;
  border-radius: 2pt; padding: 0.8mm 2mm; }
.solution-back { margin: 2mm 0 0; font-size: 8.5pt;
  font-family:"DejaVu Sans",sans-serif; text-align: left; }
.pdf-solution.in-appendix { page-break-inside: avoid; margin-bottom: 4mm; }
.pdf-solution.in-appendix::before { content: "Solution"; }
h3.sol-lesson { font-size: 12pt; margin: 6mm 0 2mm; padding-bottom: 1mm;
  border-bottom: 0.5pt solid #ccd2d8; bookmark-level: 2; }
#solutions-appendix h3.sol-lesson:first-of-type { margin-top: 0; }

table { border-collapse: collapse; margin: 3mm 0; font-size: 9.3pt; width:100%; }
th, td { border: 0.5pt solid #ccd2d8; padding: 1.2mm 2mm; text-align:left;
         vertical-align: top; }
th { background:#eef1f4; }
blockquote { border-left: 2pt solid #ccd2d8; margin: 3mm 0; padding-left: 3mm; color:#444; }
hr { border:0; border-top: 0.5pt solid #dde; margin: 4mm 0; }
"""


def pygments_css() -> str:
    try:
        from pygments.formatters import HtmlFormatter
        return HtmlFormatter(style="friendly").get_style_defs("pre.code")
    except Exception:
        return ""


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_html(items, fetcher, images=True, highlight_code=True, max_image_width=900,
               solutions="appendix"):
    slug_set = {i["slug"] for i in items if i["type"] == "lesson"}
    parts = ["<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
             "<title>Learn C++ (offline)</title>"
             f"<style>{CSS}\n{pygments_css()}</style></head><body>",
             "<div class='cover'><h1>Learn C++</h1>"
             "<div class='sub'>learncpp.com &mdash; complete offline edition</div>"
             f"<div class='sub' style='margin-top:4mm'>built {time.strftime('%Y-%m-%d')}</div>"
             "<div class='note'>Personal copy. Redistribution is not permitted by the site.<br>"
             "Support the author: learncpp.com/about</div></div>"]

    toc = ["<div id='toc'><h1>Contents</h1>"]
    for it in items:
        if it["type"] == "chapter":
            toc.append(f"<div class='toc-chap'>{esc(it['num'])} &mdash; {esc(it['title'])}</div>")
        else:
            num = f"<span class='num'>{esc(it['num'])}</span>" if it["num"] else ""
            toc.append(f"<div class='toc-lesson'>"
                       f"<a href='#{anchor_for(it['slug'])}'>{num}{esc(it['title'])}</a></div>")
    toc.append("</div>")
    parts.append("".join(toc))

    lessons = [i for i in items if i["type"] == "lesson"]
    bar = tqdm(total=len(lessons), desc="building", unit="lesson")
    skipped = []
    sol_sink, n_solutions = [], 0
    for it in items:
        if it["type"] == "chapter":
            parts.append(f"<div class='chapter-sep'><div class='kicker'>{esc(it['num'])}</div>"
                         f"<h1>{esc(it['title'])}</h1></div>")
            continue
        try:
            html = fetcher.get_text(it["url"])
        except Exception as e:
            skipped.append((it["url"], e))
            if bar is not None:
                bar.update(1)
            continue
        title, body = extract_lesson(html, it["url"])
        if body is None:
            skipped.append((it["url"], "no content found"))
            if bar is not None:
                bar.update(1)
            continue

        _highlight_code(body, enable=highlight_code)
        rewrite(body, it["slug"], slug_set)
        localize_images(body, fetcher, enable=images, max_w=max_image_width)
        for pre in body.find_all("pre"):
            if pre.get_text().count("\n") > 35:
                pre["class"] = pre.get("class", []) + ["long"]

        title = re.sub(r"^\s*[0-9A-Za-z]+\.[0-9A-Za-z]+\s*(?:\u2014|--|\u2013|-)\s*", "", title)
        head_num = f"<span class='lesson-num'>{esc(it['num'])} &mdash; </span>" if it["num"] else ""
        if solutions == "appendix":
            label = f"{it['num']} - {title}" if it["num"] else title
            n_solutions += relocate_solutions(body, it["slug"], sol_sink, label)
        parts.append(f"<section class='lesson' id='{anchor_for(it['slug'])}'>"
                     f"<h2>{head_num}{esc(title)}</h2>")
        parts.append(body.decode_contents())
        parts.append("</section>")
        if bar is not None:
            bar.update(1)
    if bar is not None:
        bar.close()
    for u, e in skipped:
        print(f"  skipped {u}: {e}", file=sys.stderr)

    if sol_sink:
        parts.append("<div class='chapter-sep'><div class='kicker'>Appendix</div>"
                     "<h1>Solutions</h1></div>"
                     "<section class='lesson' id='solutions-appendix'>")
        parts.append("".join(sol_sink))
        parts.append("</section>")
        print(f"  {n_solutions} solutions moved to the appendix")

    parts.append("</body></html>")
    return "".join(parts)


# ----------------------------------------------------------------------------
# 6. Rendering
# ----------------------------------------------------------------------------

def render_weasyprint(html_path: Path, pdf_path: Path):
    from weasyprint import HTML
    HTML(filename=str(html_path), base_url=str(html_path.parent)).write_pdf(
        str(pdf_path), optimize_images=True)


def render_wkhtmltopdf(html_path: Path, pdf_path: Path):
    exe = shutil.which("wkhtmltopdf") or "/usr/local/bin/wkhtmltopdf"
    if not Path(exe).exists():
        sys.exit("wkhtmltopdf not found")
    ver = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    if "patched qt" not in ver.lower():
        sys.exit("This wkhtmltopdf was built without patched Qt. It silently drops\n"
                 "--enable-internal-links and --outline, so you would get a PDF with\n"
                 "no internal links and no bookmarks. Install the .deb from the\n"
                 "wkhtmltopdf releases page, or use engine='weasyprint'.")
    subprocess.run([exe, "--enable-local-file-access", "--enable-internal-links",
                    "--outline", "--outline-depth", "3",
                    "--margin-top", "18mm", "--margin-bottom", "18mm",
                    "--margin-left", "16mm", "--margin-right", "16mm",
                    "--footer-center", "[page]", "--footer-font-size", "8",
                    "--print-media-type", "--image-quality", "80",
                    str(html_path), str(pdf_path)], check=True)


def shrink(pdf_path: Path):
    if not shutil.which("qpdf"):
        return
    tmp = pdf_path.with_suffix(".qpdf.pdf")
    try:
        subprocess.run(["qpdf", "--object-streams=generate", "--compress-streams=y",
                        "--recompress-flate", "--compression-level=9", "--linearize",
                        str(pdf_path), str(tmp)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tmp.stat().st_size < pdf_path.stat().st_size:
            tmp.replace(pdf_path)
        else:
            tmp.unlink()
    except Exception:
        if tmp.exists():
            tmp.unlink()


# ----------------------------------------------------------------------------
# 7. Entry points
# ----------------------------------------------------------------------------

def run(out="LearnCpp.pdf", cache_dir="cache", work_dir=".", engine="weasyprint",
        images=True, highlight_code=True, max_image_width=900, delay=0.4,
        workers=4, refresh=False, limit=None, html_only=False, shrink_pdf=True,
        solutions="appendix"):
    configure(cache_dir, work_dir)
    f = Fetcher(delay=delay, refresh=refresh)

    print("-> reading the table of contents...")
    items = collect_toc(f.get_text(BASE), limit=limit)
    nl = sum(1 for i in items if i["type"] == "lesson")
    nc = sum(1 for i in items if i["type"] == "chapter")
    print(f"  found {nc} chapters, {nl} lessons")
    if nl == 0:
        raise SystemExit("Could not parse the table of contents; the site markup changed.")

    print("-> downloading pages...")
    f.prefetch([i["url"] for i in items if i["type"] == "lesson"], workers=workers)

    print("-> cleaning up and assembling book.html...")
    html = build_html(items, f, images=images, highlight_code=highlight_code,
                      max_image_width=max_image_width, solutions=solutions)
    html_path = WORK / "book.html"
    html_path.write_text(html, "utf-8")
    print(f"  book.html: {html_path.stat().st_size/1e6:.1f} MB")
    if html_only:
        return html_path

    pdf = Path(out)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    print(f"-> rendering PDF ({engine})...")
    t0 = time.time()
    (render_weasyprint if engine == "weasyprint" else render_wkhtmltopdf)(html_path, pdf)
    if shrink_pdf:
        shrink(pdf)
    print(f"done: {pdf} - {pdf.stat().st_size/1e6:.1f} MB in {time.time()-t0:.0f}s")
    return pdf


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-o", "--out", default="LearnCpp.pdf")
    p.add_argument("--cache-dir", default="cache")
    p.add_argument("--work-dir", default=".")
    p.add_argument("--engine", choices=["weasyprint", "wkhtmltopdf"], default="weasyprint")
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--no-highlight", action="store_true")
    p.add_argument("--max-image-width", type=int, default=900)
    p.add_argument("--delay", type=float, default=0.4)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--html-only", action="store_true")
    p.add_argument("--no-shrink", action="store_true")
    p.add_argument("--solutions", choices=["appendix", "inline"], default="appendix",
                   help="appendix: quiz answers move to the back of the book behind "
                        "a link; inline: printed right under the question")
    a = p.parse_args()
    run(out=a.out, cache_dir=a.cache_dir, work_dir=a.work_dir, engine=a.engine,
        images=not a.no_images, highlight_code=not a.no_highlight,
        max_image_width=a.max_image_width, delay=a.delay, workers=a.workers,
        refresh=a.refresh, limit=a.limit, html_only=a.html_only,
        shrink_pdf=not a.no_shrink, solutions=a.solutions)


if __name__ == "__main__":
    main()
