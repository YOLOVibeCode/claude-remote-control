#!/usr/bin/env python3
"""Turn `tmux capture-pane -e -p` output into a macOS-window-styled HTML page.
usage: ansi2html.py in.ansi out.html "Window title" [highlight-regex ...]"""
import html, re, sys

BASE16 = ["#1e1e2e", "#f38ba8", "#a6e3a1", "#f9e2af", "#89b4fa", "#f5c2e7", "#94e2d5", "#bac2de",
          "#585b70", "#f38ba8", "#a6e3a1", "#f9e2af", "#89b4fa", "#f5c2e7", "#94e2d5", "#ffffff"]
FG, BG = "#cdd6f4", "#1e1e2e"


def c256(n):
    if n < 16:
        return BASE16[n]
    if n < 232:
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (steps[n // 36], steps[(n // 6) % 6], steps[n % 6])
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def convert(text):
    out, st = [], {}
    pos = 0
    for m in re.finditer(r"\x1b\[([0-9;:]*)m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]", text):
        out.append(span(text[pos:m.start()], st))
        pos = m.end()
        if m.group(1) is None:
            continue
        codes = [int(c) if c else 0 for c in re.split("[;:]", m.group(1))] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                st = {}
            elif c == 1: st["b"] = 1
            elif c == 2: st["dim"] = 1
            elif c == 3: st["i"] = 1
            elif c == 4: st["u"] = 1
            elif c == 7: st["rev"] = 1
            elif c == 22: st.pop("b", None); st.pop("dim", None)
            elif c == 23: st.pop("i", None)
            elif c == 24: st.pop("u", None)
            elif c == 27: st.pop("rev", None)
            elif 30 <= c <= 37: st["fg"] = BASE16[c - 30]
            elif 90 <= c <= 97: st["fg"] = BASE16[c - 82]
            elif 40 <= c <= 47: st["bg"] = BASE16[c - 40]
            elif 100 <= c <= 107: st["bg"] = BASE16[c - 92]
            elif c == 39: st.pop("fg", None)
            elif c == 49: st.pop("bg", None)
            elif c in (38, 48) and i + 1 < len(codes):
                key = "fg" if c == 38 else "bg"
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    st[key] = c256(codes[i + 2]); i += 2
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    st[key] = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5]); i += 4
            i += 1
    out.append(span(text[pos:], st))
    return "".join(out)


def span(s, st):
    if not s:
        return ""
    s = html.escape(s)
    if not st:
        return s
    fg, bg = st.get("fg"), st.get("bg")
    if st.get("rev"):
        fg, bg = bg or BG, fg or FG
    css = []
    if fg: css.append("color:" + fg)
    if bg: css.append("background:" + bg)
    if st.get("b"): css.append("font-weight:700")
    if st.get("dim"): css.append("opacity:.6")
    if st.get("i"): css.append("font-style:italic")
    if st.get("u"): css.append("text-decoration:underline")
    return '<span style="%s">%s</span>' % (";".join(css), s)


def main():
    src, dst, title = sys.argv[1:4]
    marks = sys.argv[4:]
    text = open(src, encoding="utf-8", errors="replace").read().rstrip("\n")
    # never publish a real Remote Control session id
    text = re.sub(r"session_[A-Za-z0-9]{12,}", "session_01AbCdEfGhIjKlMnOpQrSt", text)
    text = re.sub(r"env_[A-Za-z0-9]{12,}", "env_01AbCdEfGhIjKlMnOpQrSt", text)
    # collapse long runs of blank rows so the window is not mostly empty
    text = re.sub(r"(\n(?:[ \t]*(?:\x1b\[[0-9;:]*m)*[ \t]*)){4,}\n", "\n\n\n", text)
    body = convert(text)
    for mk in marks:
        body = re.sub(mk, lambda m: '<mark>%s</mark>' % m.group(0), body)
    page = f"""<!doctype html><meta charset=utf-8>
<style>
body{{margin:0;padding:36px;background:linear-gradient(135deg,#6d5dfc,#c86dd7 55%,#ff9a8b);display:inline-block}}
.win{{border-radius:12px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.45);background:{BG};display:inline-block}}
.bar{{height:34px;background:#2a2a3c;display:flex;align-items:center;padding:0 14px;gap:8px;position:relative}}
.dot{{width:12px;height:12px;border-radius:50%}}
.t{{position:absolute;left:0;right:0;text-align:center;color:#a6adc8;font:13px -apple-system,Helvetica,sans-serif}}
pre{{margin:0;padding:16px 18px;color:{FG};font:14px/1.22 "SF Mono",Menlo,monospace;white-space:pre}}
mark{{background:transparent;color:inherit;outline:2px solid #ff5e7e;outline-offset:2px;border-radius:3px}}
</style>
<div class=win><div class=bar><span class=dot style="background:#ff5f57"></span><span class=dot style="background:#febc2e"></span><span class=dot style="background:#28c840"></span><span class=t>{html.escape(title)}</span></div><pre>{body}</pre></div>"""
    open(dst, "w").write(page)


main()
