"""Just enough markdown for brain notes: headings, lists, code, bold, links, and [[kind/slug|title]]
links between notes. Everything is escaped first, so a note can never put markup on the page."""

import re

import markupsafe

WIKI = re.compile(r"\[\[([a-z][a-z0-9-]*/[a-z0-9][a-z0-9-]*)(?:\|([^\]]+))?\]\]")
LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline(text, note_url):
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<code>{markupsafe.escape(part[1:-1])}</code>")
            continue
        s = str(markupsafe.escape(part))
        s = WIKI.sub(lambda m: f'<a href="{note_url(m.group(1))}">{m.group(2) or m.group(1)}</a>', s)
        s = LINK.sub(lambda m: f'<a href="{m.group(2)}" rel="noreferrer noopener" target="_blank">{m.group(1)}</a>', s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        out.append(s)
    return "".join(out)


def render(text, note_url):
    """note_url(note_id) -> the page for that note."""
    html, para, items, ordered, code = [], [], [], False, None

    def flush():
        nonlocal items
        if para:
            html.append(f"<p>{_inline(' '.join(para), note_url)}</p>")
            para.clear()
        if items:
            tag = "ol" if ordered else "ul"
            html.append(f"<{tag}>" + "".join(f"<li>{_inline(i, note_url)}</li>" for i in items) + f"</{tag}>")
            items = []

    for line in (text or "").splitlines():
        if code is not None:
            if line.startswith("```"):
                html.append(f"<pre><code>{markupsafe.escape(chr(10).join(code))}</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            code = []
        elif not stripped:
            flush()
        elif m := re.match(r"(#{1,6})\s+(.*)", stripped):
            flush()
            level = min(6, len(m.group(1)) + 2)
            html.append(f"<h{level}>{_inline(m.group(2), note_url)}</h{level}>")
        elif m := re.match(r"(?:[-*]|(\d+)[.)])\s+(.*)", stripped):
            if para:
                flush()
            if items and ordered != bool(m.group(1)):
                flush()
            ordered = bool(m.group(1))
            items.append(m.group(2))
        elif items and line.startswith((" ", "\t")):
            items[-1] += " " + stripped
        else:
            if items:
                flush()
            para.append(stripped)
    if code is not None:
        html.append(f"<pre><code>{markupsafe.escape(chr(10).join(code))}</code></pre>")
    flush()
    return markupsafe.Markup("".join(html))
