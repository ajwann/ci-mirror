"""A small YAML subset parser for CI files (stdlib only). PyYAML is used instead when importable.

Supports block maps and lists (including `- key: value` items), plain / quoted scalars, block
scalars (| |- |+ > >-), simple flow lists `[a, b]`, comments, and document markers. Anchors are
stripped and aliases are kept as strings. Everything scalar comes back as a string except
true/false/null and integers. That is enough to read workflow / pipeline files.
"""
from __future__ import annotations

import re
from typing import Any

try:  # pragma: no cover - depends on the environment
    import yaml as _pyyaml  # type: ignore
except Exception:  # noqa: BLE001
    _pyyaml = None


def load(text: str, force_mini: bool = False) -> Any:
    if _pyyaml is not None and not force_mini:
        try:
            return _pyyaml.safe_load(text)
        except Exception:  # noqa: BLE001 - fall back to the subset parser
            pass
    return _Parser(text).parse()


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"" and (i == 0 or line[i - 1] in " :[{,-"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(v: str) -> Any:
    v = v.strip()
    v = re.sub(r"^&\S+\s*", "", v)  # anchor
    if not v:
        return None
    if (v[0] == v[-1]) and v[0] in "'\"" and len(v) >= 2:
        inner = v[1:-1]
        if v[0] == "'":
            return inner.replace("''", "'")
        return inner.replace("\\\\", "\x00").replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\x00", "\\")
    if v.startswith("[") and v.endswith("]"):
        body = v[1:-1].strip()
        return [_scalar(x) for x in _split_flow(body)] if body else []
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _split_flow(body: str) -> list[str]:
    parts, depth, cur, quote = [], 0, [], None
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


_KEY_RE = re.compile(r"^(?P<key>\"[^\"]*\"|'[^']*'|[^\s:#][^:#]*?|<<)\s*:(?:\s+(?P<val>.*)|$)")


class _Parser:
    def __init__(self, text: str):
        self.raw = text.replace("\t", "  ").splitlines()
        self.i = 0

    # ---- line access
    def _next_content(self) -> int:
        """Index of the next non-blank, non-comment line (or len)."""
        j = self.i
        while j < len(self.raw):
            s = self.raw[j].strip()
            if s and not s.startswith("#") and s not in ("---", "..."):
                return j
            j += 1
        return j

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def parse(self) -> Any:
        j = self._next_content()
        if j >= len(self.raw):
            return None
        self.i = j
        return self._block(self._indent(self.raw[j]))

    def _block(self, indent: int) -> Any:
        j = self._next_content()
        if j >= len(self.raw):
            return None
        line = self.raw[j]
        if line.lstrip().startswith("- ") or line.strip() == "-":
            return self._list(indent)
        return self._map(indent)

    def _block_scalar(self, header: str, parent_indent: int) -> str:
        style, chomp = header[0], header[1:2]
        lines, ind = [], None
        while self.i < len(self.raw):
            line = self.raw[self.i]
            if line.strip() == "":
                lines.append("")
                self.i += 1
                continue
            cur = self._indent(line)
            if cur <= parent_indent:
                break
            if ind is None:
                ind = cur
            if cur < ind:
                break
            lines.append(line[ind:])
            self.i += 1
        while lines and lines[-1] == "" and chomp != "+":
            lines.pop()
        if style == ">":
            text, para = [], []
            for l in lines:
                if l == "":
                    text.append(" ".join(para))
                    para = []
                elif l.startswith(" "):
                    if para:
                        text.append(" ".join(para))
                        para = []
                    text.append(l)
                else:
                    para.append(l)
            if para:
                text.append(" ".join(para))
            body = "\n".join(text)
        else:
            body = "\n".join(lines)
        return body if chomp == "-" else body + "\n"

    def _value(self, val: str, indent: int) -> Any:
        """Value after `key:` or `- ` on a line already consumed; may read a nested block."""
        val = _strip_comment(val).strip() if val else ""
        if val[:1] in "|>" and re.fullmatch(r"[|>][-+]?\d*", val):
            return self._block_scalar(val, indent)
        if val:
            return _scalar(val)
        j = self._next_content()
        if j < len(self.raw):
            nxt = self.raw[j]
            ni = self._indent(nxt)
            if ni > indent or (ni == indent and nxt.lstrip().startswith("- ")):
                self.i = j
                return self._block(ni)
        return None

    def _map(self, indent: int) -> dict:
        out: dict = {}
        while True:
            j = self._next_content()
            if j >= len(self.raw):
                break
            line = self.raw[j]
            ind = self._indent(line)
            if ind != indent or line.lstrip().startswith("- "):
                break
            m = _KEY_RE.match(_strip_comment(line.strip()) if "#" in line else line.strip())
            if not m:
                self.i = j + 1
                continue
            key = m.group("key").strip().strip("'\"")
            self.i = j + 1
            value = self._value(m.group("val") or "", indent)
            if key == "<<" and isinstance(value, dict):
                for k, v in value.items():
                    out.setdefault(k, v)
            else:
                out[key] = value
        return out

    def _list(self, indent: int) -> list:
        out: list = []
        while True:
            j = self._next_content()
            if j >= len(self.raw):
                break
            line = self.raw[j]
            ind = self._indent(line)
            stripped = line.lstrip()
            if ind != indent or not (stripped.startswith("- ") or stripped == "-"):
                break
            rest = stripped[1:].lstrip() if stripped.startswith("- ") else ""
            item_indent = ind + len(stripped) - len(rest)
            self.i = j + 1
            m = _KEY_RE.match(_strip_comment(rest)) if rest and not rest[:1] in "'\"[{|>" else None
            if m:
                # `- key: value` starts a map whose other keys sit at item_indent
                self.raw[j] = " " * item_indent + rest
                self.i = j
                out.append(self._map(item_indent))
            else:
                out.append(self._value(rest, ind))
        return out
