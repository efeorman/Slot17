#!/usr/bin/env python3
"""Convert Markdown files to print-ready PDFs (Chrome headless renderer).

Usage:
    python md2pdf.py                 # convert every *.md in this folder
    python md2pdf.py FILE.md [...]   # convert specific files

Each FILE.md produces FILE.pdf next to it.
Requires: pip install markdown   (Chrome or Edge must be installed)
"""
import os
import re
import subprocess
import sys
import tempfile

import markdown

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: "Segoe UI", -apple-system, "Helvetica Neue", Arial, sans-serif;
  font-size: 10pt; line-height: 1.55; color: #1a1a1a; margin: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1, h2, h3, h4 { line-height: 1.25; break-after: avoid; page-break-after: avoid; }
h1 { font-size: 21pt; margin: 0 0 .5em; padding-bottom: .25em;
     border-bottom: 2.5px solid #1a3a5c; color: #10263d; }
h2 { font-size: 15pt; margin: 1.7em 0 .55em; padding-bottom: .18em;
     border-bottom: 1px solid #d0d7de; color: #10263d;
     break-before: auto; page-break-before: auto; }
h3 { font-size: 12pt; margin: 1.3em 0 .4em; color: #1a3a5c; }
h4 { font-size: 10.5pt; margin: 1.1em 0 .35em; color: #33475b; }
p, ul, ol { margin: 0 0 .7em; }
li { margin-bottom: .22em; }
li > ul, li > ol { margin-top: .22em; }
a { color: #14507d; text-decoration: none; word-break: break-word; }
strong { color: #10263d; }
hr { border: 0; border-top: 1px solid #dde3ea; margin: 1.8em 0; }

table {
  border-collapse: collapse; width: 100%; margin: .8em 0 1.2em;
  font-size: 8.6pt; break-inside: auto;
}
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th, td {
  border: 1px solid #cdd5de; padding: 5px 8px;
  text-align: left; vertical-align: top;
}
th { background: #eef2f6; font-weight: 600; color: #10263d; }
tbody tr:nth-child(even) td { background: #fafbfc; }

code {
  font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
  font-size: .87em; background: #f2f4f7; padding: 1px 4px;
  border-radius: 3px; border: 1px solid #e3e8ee;
}
pre {
  background: #f7f9fb; border: 1px solid #dde3ea; border-left: 3px solid #1a3a5c;
  border-radius: 4px; padding: 10px 12px; overflow-x: auto;
  font-size: 8.3pt; line-height: 1.4; break-inside: avoid; page-break-inside: avoid;
}
pre code { background: none; border: 0; padding: 0; font-size: inherit; }

blockquote {
  margin: .9em 0; padding: .55em .9em; background: #f5f8fb;
  border-left: 3px solid #4a7ba7; color: #2c3e50; break-inside: avoid;
}
blockquote p:last-child { margin-bottom: 0; }

img { max-width: 100%; }
"""

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>{css}</style></head><body>{body}</body></html>"""


def find_browser():
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    raise SystemExit("No Chrome or Edge installation found; cannot render PDF.")


def convert(md_path, browser):
    md_path = os.path.abspath(md_path)
    pdf_path = os.path.splitext(md_path)[0] + ".pdf"
    text = open(md_path, encoding="utf-8").read()

    body = markdown.markdown(
        text,
        extensions=["extra", "sane_lists", "toc", "attr_list"],
        extension_configs={"toc": {"permalink": False}},
    )
    # Markdown requires a header row; drop it again when every cell is empty.
    body = re.sub(
        r"<thead>\s*<tr>(?:\s*<th[^>]*>\s*</th>)+\s*</tr>\s*</thead>", "", body
    )

    title = os.path.splitext(os.path.basename(md_path))[0]
    # First H1 makes a nicer document title if present.
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()

    fd, html_path = tempfile.mkstemp(suffix=".html", dir=os.path.dirname(md_path))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(HTML.format(title=title, css=CSS, body=body))

    try:
        subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--no-pdf-header-footer",
                "--virtual-time-budget=10000",
                f"--print-to-pdf={pdf_path}",
                "file:///" + html_path.replace("\\", "/"),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
    finally:
        os.remove(html_path)

    size = os.path.getsize(pdf_path)
    print(f"  {os.path.basename(md_path)} -> {os.path.basename(pdf_path)} ({size/1024:.0f} KB)")


def main():
    args = sys.argv[1:]
    if not args:
        here = os.path.dirname(os.path.abspath(__file__))
        args = sorted(
            os.path.join(here, f) for f in os.listdir(here) if f.lower().endswith(".md")
        )
    if not args:
        raise SystemExit("No Markdown files found.")

    browser = find_browser()
    print(f"Renderer: {os.path.basename(browser)}")
    for path in args:
        convert(path, browser)


if __name__ == "__main__":
    main()
