"""YAML 1.2 subset used by the ledger.

Python's stdlib has no YAML parser, and mil's runtime is stdlib-only.
The ledger only needs maps, lists, scalars, and `|` / `>` strings; YAML
features outside this subset (anchors, aliases, tags, merge keys, and
multiline plain scalars) raise YAMLError instead of degrading silently.
"""

from __future__ import annotations

import re
from typing import Any


class YAMLError(ValueError):
    pass


_DOUBLE_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}


class _Line:
    __slots__ = ("lineno", "indent", "content", "blank", "comment", "raw")

    def __init__(
        self, lineno: int, indent: int, content: str, blank: bool, comment: bool, raw: str
    ) -> None:
        self.lineno = lineno
        self.indent = indent
        self.content = content
        self.blank = blank
        self.comment = comment
        # The untouched line. Block scalars read this: inside `|` and `>`
        # a `#` is text, not a comment, and tabs are allowed.
        self.raw = raw


def load(text: str) -> Any:
    if text is None:
        return None
    if not str(text).strip():
        return None
    lines = _tokenize(text)
    parser = _Parser(lines)
    return parser.parse_document()


def load_path(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return load(fh.read())


def dump(value: Any) -> str:
    return _dump_value(value, 0) + "\n"


def dump_path(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump(value))


def _tokenize(text: str) -> list[_Line]:
    # Split on "\n" only. str.splitlines() also breaks on form feed,
    # U+2028 and the like, which may sit inside a string.
    raws = text.split("\n")
    if raws and raws[-1] == "":
        raws.pop()
    lines: list[_Line] = []
    for lineno, raw in enumerate(raws, 1):
        if raw.endswith("\r"):
            raw = raw[:-1]
        if raw.strip() == "":
            lines.append(_Line(lineno, 0, "", True, False, raw))
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw[indent:]
        if content.startswith("#"):
            lines.append(_Line(lineno, indent, "", False, True, raw))
            continue
        content = _strip_unquoted_comment(content)
        if content == "":
            lines.append(_Line(lineno, indent, "", False, True, raw))
            continue
        lines.append(_Line(lineno, indent, content, False, False, raw))
    return lines


def _strip_unquoted_comment(content: str) -> str:
    quote = None
    escaped = False
    for i, ch in enumerate(content):
        if quote:
            if escaped:
                escaped = False
                continue
            if ch == "\\" and quote == '"':
                escaped = True
                continue
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "#" and (i == 0 or content[i - 1].isspace()):
            return content[:i].rstrip()
    return content.rstrip()


class _Parser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.i = 0

    def parse_document(self) -> Any:
        self._skip_blanks_and_comments()
        line = self._peek()
        if line is None:
            return None
        if line.content in ("---", "..."):
            self._advance()
            self._skip_blanks_and_comments()
        if self._peek() is None:
            return None
        value = self._parse_node(min_indent=0)
        self._skip_blanks_and_comments()
        tail = self._peek()
        if tail is not None and tail.content not in ("...", "---"):
            raise YAMLError(f"line {tail.lineno}: unexpected content {tail.content!r}")
        return value

    def _peek(self) -> _Line | None:
        if self.i >= len(self.lines):
            return None
        line = self.lines[self.i]
        # Tabs are rejected here, not in _tokenize, so that block scalar
        # lines (which never pass through _peek) may contain them.
        if "\t" in line.raw and not line.blank and not line.comment:
            raise YAMLError(f"line {line.lineno}: tabs are not allowed")
        return line

    def _advance(self) -> _Line:
        line = self.lines[self.i]
        self.i += 1
        return line

    def _skip_blanks_and_comments(self) -> None:
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.blank or line.comment:
                self.i += 1
                continue
            break

    def _parse_node(self, min_indent: int) -> Any:
        self._skip_blanks_and_comments()
        line = self._peek()
        if line is None:
            return None
        if line.indent < min_indent:
            return None
        if line.content.startswith("- ") or line.content == "-":
            return self._parse_sequence(line.indent)
        if line.content.startswith("{") or line.content.startswith("["):
            self._advance()
            return _parse_flow(line.content, line.lineno)
        return self._parse_mapping(line.indent)

    def _parse_mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            self._skip_blanks_and_comments()
            line = self._peek()
            if line is None or line.indent < indent:
                break
            if line.indent > indent:
                raise YAMLError(
                    f"line {line.lineno}: unexpected indentation (multiline plain scalars must use '|' or '>')"
                )
            if line.content.startswith("- ") or line.content == "-":
                break
            key, rest, lineno = self._split_key_line(line)
            if key in result:
                raise YAMLError(f"line {lineno}: duplicate key {key!r}")
            self._advance()
            result[key] = self._parse_map_value(rest, indent, lineno)
        return result

    def _parse_map_value(self, rest: str | None, key_indent: int, lineno: int) -> Any:
        if rest is None or rest == "":
            self._skip_blanks_and_comments()
            nxt = self._peek()
            if nxt is None or nxt.indent <= key_indent:
                return None
            if nxt.content.startswith("- ") or nxt.content == "-":
                return self._parse_sequence(nxt.indent)
            return self._parse_mapping(nxt.indent)
        if rest in ("|", "|-", "|+", ">", ">-", ">+"):
            return self._parse_block_scalar(rest[0], key_indent, chomp=rest[1:] if len(rest) > 1 else "")
        if rest.startswith("{") or rest.startswith("["):
            return _parse_flow(rest, lineno)
        return parse_scalar(rest, lineno)

    def _parse_sequence(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while True:
            self._skip_blanks_and_comments()
            line = self._peek()
            if line is None or line.indent < indent:
                break
            if line.indent > indent:
                raise YAMLError(
                    f"line {line.lineno}: unexpected indentation (multiline plain scalars must use '|' or '>')"
                )
            if not (line.content.startswith("- ") or line.content == "-"):
                break
            self._advance()
            raw_after_dash = line.content[1:]
            if raw_after_dash == "":
                item_text = ""
                key_indent = indent + 2
            else:
                lead = len(raw_after_dash) - len(raw_after_dash.lstrip(" "))
                item_text = raw_after_dash.lstrip()
                key_indent = indent + 1 + max(1, lead)
            if item_text == "":
                self._skip_blanks_and_comments()
                nxt = self._peek()
                if nxt is None or nxt.indent <= indent:
                    result.append(None)
                else:
                    result.append(self._parse_node(min_indent=nxt.indent))
                continue
            if item_text == "-" or item_text.startswith("- "):
                idx = self.i - 1
                orig = self.lines[idx]
                self.lines[idx] = _Line(
                    line.lineno,
                    key_indent,
                    item_text,
                    False,
                    False,
                    " " * key_indent + item_text,
                )
                self.i = idx
                try:
                    result.append(self._parse_sequence(key_indent))
                finally:
                    self.lines[idx] = orig
                continue
            if item_text in ("|", "|-", "|+", ">", ">-", ">+"):
                result.append(
                    self._parse_block_scalar(item_text[0], indent, chomp=item_text[1:] if len(item_text) > 1 else "")
                )
                continue
            if item_text.startswith("{") or item_text.startswith("["):
                result.append(_parse_flow(item_text, line.lineno))
                continue
            if _looks_like_key(item_text):
                key, rest, _lineno = _split_key(item_text, line.lineno)
                mapping: dict[str, Any] = {}
                mapping[key] = self._parse_map_value(rest, key_indent, line.lineno)
                while True:
                    self._skip_blanks_and_comments()
                    nxt = self._peek()
                    if nxt is None or nxt.indent < key_indent:
                        if (
                            nxt is not None
                            and nxt.indent > indent
                            and not (nxt.content.startswith("- ") or nxt.content == "-")
                        ):
                            raise YAMLError(f"line {nxt.lineno}: unexpected indentation in sequence mapping")
                        break
                    if nxt.indent > key_indent:
                        raise YAMLError(
                            f"line {nxt.lineno}: unexpected indentation (multiline plain scalars must use '|' or '>')"
                        )
                    if nxt.content.startswith("- ") or nxt.content == "-":
                        break
                    nkey, nrest, nlineno = self._split_key_line(nxt)
                    if nkey in mapping:
                        raise YAMLError(f"line {nlineno}: duplicate key {nkey!r}")
                    self._advance()
                    mapping[nkey] = self._parse_map_value(nrest, key_indent, nlineno)
                result.append(mapping)
                continue
            result.append(parse_scalar(item_text, line.lineno))
        return result

    def _parse_block_scalar(self, style: str, key_indent: int, chomp: str) -> str:
        # Read raw lines: everything indented deeper than the key is text,
        # including `#` lines, ` # ...` tails and tabs.
        collected: list[tuple[int, str]] = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.blank:
                collected.append((0, ""))
                self.i += 1
                continue
            if line.indent <= key_indent:
                break
            collected.append((line.indent, line.raw[line.indent :]))
            self.i += 1
        while collected and collected[-1][1] == "":
            collected.pop()
        if not collected:
            return ""
        nonempty = [ind for ind, txt in collected if txt != ""]
        strip_to = min(nonempty) if nonempty else key_indent + 2
        body_lines: list[str] = []
        for ind, txt in collected:
            if txt == "":
                body_lines.append("")
            else:
                extra = max(0, ind - strip_to)
                body_lines.append(" " * extra + txt)
        if style == "|":
            text = "\n".join(body_lines)
        else:
            text = _fold(body_lines)
        if chomp == "+":
            if not text.endswith("\n"):
                text += "\n"
            return text
        if chomp == "-":
            return text.rstrip("\n")
        if text == "":
            return ""
        return text.rstrip("\n") + "\n"

    def _split_key_line(self, line: _Line) -> tuple[str, str | None, int]:
        return _split_key(line.content, line.lineno)


def _looks_like_key(text: str) -> bool:
    if text.startswith("{") or text.startswith("["):
        return False
    if text.startswith("'") or text.startswith('"'):
        try:
            _split_key(text, 0)
            return True
        except YAMLError:
            return False
    if ": " in text or text.endswith(":"):
        return True
    return False


def _split_key(content: str, lineno: int) -> tuple[str, str | None, int]:
    quote = None
    escaped = False
    for i, ch in enumerate(content):
        if quote:
            if escaped:
                escaped = False
                continue
            if ch == "\\" and quote == '"':
                escaped = True
                continue
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"') and i == 0:
            quote = ch
            continue
        if ch == ":":
            rest = content[i + 1 :]
            if rest == "":
                key = _unquote_key(content[:i], lineno)
                return key, None, lineno
            if rest[0].isspace():
                key = _unquote_key(content[:i], lineno)
                return key, rest.strip() or None, lineno
            # `http://` inside an unquoted value is not a key split; keys
            # cannot contain unquoted colons without a following space.
            continue
    raise YAMLError(f"line {lineno}: expected a mapping key, got {content!r}")


def _unquote_key(raw: str, lineno: int) -> str:
    key = raw.strip()
    if key.startswith("'") or key.startswith('"'):
        value = parse_scalar(key, lineno)
        if not isinstance(value, str):
            raise YAMLError(f"line {lineno}: mapping key must be a string")
        return value
    if key == "":
        raise YAMLError(f"line {lineno}: empty mapping key")
    if key == "<<":
        raise YAMLError(f"line {lineno}: YAML merge key '<<' is not supported")
    if key[0] in ("&", "*", "!", "@", "`"):
        raise YAMLError(f"line {lineno}: unsupported YAML indicator {key[0]!r} in mapping key")
    return key


def _fold(lines: list[str]) -> str:
    paragraphs: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            paragraphs.append(" ".join(buf))
            buf.clear()

    for line in lines:
        if line == "":
            flush()
            paragraphs.append("")
        else:
            buf.append(line.strip())
    flush()
    text = ""
    for part in paragraphs:
        if part == "":
            text += "\n"
        else:
            if text and not text.endswith("\n"):
                text += " "
            text += part
    if lines:
        text += "\n"
    return text


def _line_prefix(lineno: int) -> str:
    return f"line {lineno}: " if lineno > 0 else ""


def parse_scalar(text: str, lineno: int = 0) -> Any:
    if text == "" or text in ("null", "Null", "NULL", "~"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    # YAML 1.1 would turn yes/no into booleans. classification.decidable
    # uses those words, so they stay strings.
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return _decode_double(text[1:-1], lineno)
    if text[0] == "&":
        raise YAMLError(f"{_line_prefix(lineno)}YAML anchors ('&...') are not supported; quote the string if literal")
    if text[0] == "*":
        raise YAMLError(f"{_line_prefix(lineno)}YAML aliases ('*...') are not supported; quote the string if literal")
    if text[0] == "!":
        raise YAMLError(f"{_line_prefix(lineno)}YAML tags ('!...') are not supported; quote the string if literal")
    if text[0] in ("@", "`"):
        raise YAMLError(
            f"{_line_prefix(lineno)}reserved YAML indicator {text[0]!r} cannot start an unquoted scalar"
        )
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"-?(0|[1-9][0-9]*)\.[0-9]+([eE][-+]?[0-9]+)?", text) or re.fullmatch(
        r"-?(0|[1-9][0-9]*)[eE][-+]?[0-9]+", text
    ):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _decode_double(inner: str, lineno: int = 0) -> str:
    out: list[str] = []
    i = 0
    prefix = _line_prefix(lineno)
    while i < len(inner):
        ch = inner[i]
        if ch == "\\":
            if i + 1 >= len(inner):
                raise YAMLError(f"{prefix}trailing backslash in double-quoted string")
            nxt = inner[i + 1]
            if nxt in _DOUBLE_ESCAPES:
                out.append(_DOUBLE_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "x":
                hex_part = inner[i + 2 : i + 4]
                if len(hex_part) == 2 and re.fullmatch(r"[0-9a-fA-F]{2}", hex_part):
                    out.append(chr(int(hex_part, 16)))
                    i += 4
                    continue
                raise YAMLError(f"{prefix}invalid \\x escape in double-quoted string")
            if nxt == "u":
                hex_part = inner[i + 2 : i + 6]
                if len(hex_part) == 4 and re.fullmatch(r"[0-9a-fA-F]{4}", hex_part):
                    cp = int(hex_part, 16)
                    if 0xD800 <= cp <= 0xDFFF:
                        raise YAMLError(f"{prefix}surrogate code point \\u{hex_part} is not valid Unicode")
                    out.append(chr(cp))
                    i += 6
                    continue
                raise YAMLError(f"{prefix}invalid \\u escape in double-quoted string")
            if nxt == "U":
                hex_part = inner[i + 2 : i + 10]
                if len(hex_part) == 8 and re.fullmatch(r"[0-9a-fA-F]{8}", hex_part):
                    cp = int(hex_part, 16)
                    if cp > 0x10FFFF or (0xD800 <= cp <= 0xDFFF):
                        raise YAMLError(f"{prefix}invalid Unicode code point \\U{hex_part}")
                    out.append(chr(cp))
                    i += 10
                    continue
                raise YAMLError(f"{prefix}invalid \\U escape in double-quoted string")
            raise YAMLError(f"{prefix}unknown escape sequence \\{nxt} in double-quoted string")
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_flow(text: str, lineno: int) -> Any:
    value, end = _consume_flow(text, 0, lineno)
    tail = text[end:].strip()
    if tail:
        raise YAMLError(f"line {lineno}: unexpected trailing content {tail!r}")
    return value


def _consume_flow(text: str, i: int, lineno: int) -> tuple[Any, int]:
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        raise YAMLError(f"line {lineno}: truncated flow value")
    if text[i] == "{":
        return _consume_flow_map(text, i + 1, lineno)
    if text[i] == "[":
        return _consume_flow_seq(text, i + 1, lineno)
    return _consume_flow_scalar(text, i, lineno)


def _consume_flow_map(text: str, i: int, lineno: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    i = _skip_ws(text, i)
    if i < len(text) and text[i] == "}":
        return result, i + 1
    while True:
        i = _skip_ws(text, i)
        key, i = _consume_flow_key(text, i, lineno)
        i = _skip_ws(text, i)
        if i >= len(text) or text[i] != ":":
            raise YAMLError(f"line {lineno}: expected ':' in flow mapping")
        i += 1
        value, i = _consume_flow(text, i, lineno)
        if key in result:
            raise YAMLError(f"line {lineno}: duplicate key {key!r}")
        result[key] = value
        i = _skip_ws(text, i)
        if i >= len(text):
            raise YAMLError(f"line {lineno}: unterminated flow mapping")
        if text[i] == "}":
            return result, i + 1
        if text[i] != ",":
            raise YAMLError(f"line {lineno}: expected ',' or '}}' in flow mapping")
        i += 1


def _consume_flow_seq(text: str, i: int, lineno: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    i = _skip_ws(text, i)
    if i < len(text) and text[i] == "]":
        return result, i + 1
    while True:
        value, i = _consume_flow(text, i, lineno)
        result.append(value)
        i = _skip_ws(text, i)
        if i >= len(text):
            raise YAMLError(f"line {lineno}: unterminated flow sequence")
        if text[i] == "]":
            return result, i + 1
        if text[i] != ",":
            raise YAMLError(f"line {lineno}: expected ',' or ']' in flow sequence")
        i += 1


def _consume_flow_key(text: str, i: int, lineno: int) -> tuple[str, int]:
    i = _skip_ws(text, i)
    quoted = i < len(text) and text[i] in ("'", '"')
    value, end = _consume_flow_scalar(text, i, lineno, stop_at_colon=True)
    key = value if isinstance(value, str) else str(value)
    if not quoted and key == "<<":
        raise YAMLError(f"line {lineno}: YAML merge key '<<' is not supported")
    return key, end


def _consume_flow_scalar(
    text: str, i: int, lineno: int, stop_at_colon: bool = False
) -> tuple[Any, int]:
    i = _skip_ws(text, i)
    if i >= len(text):
        raise YAMLError(f"line {lineno}: truncated scalar")
    if text[i] == '"':
        j = i + 1
        escaped = False
        while j < len(text):
            ch = text[j]
            if escaped:
                escaped = False
                j += 1
                continue
            if ch == "\\":
                escaped = True
                j += 1
                continue
            if ch == '"':
                return parse_scalar(text[i : j + 1], lineno), j + 1
            j += 1
        raise YAMLError(f"line {lineno}: unterminated double quote")
    if text[i] == "'":
        j = i + 1
        while j < len(text):
            if text[j] == "'" and j + 1 < len(text) and text[j + 1] == "'":
                j += 2
                continue
            if text[j] == "'":
                return parse_scalar(text[i : j + 1], lineno), j + 1
            j += 1
        raise YAMLError(f"line {lineno}: unterminated single quote")
    j = i
    while j < len(text):
        ch = text[j]
        if ch in ",]}":
            break
        if stop_at_colon and ch == ":":
            break
        if ch in "{[":
            raise YAMLError(f"line {lineno}: unexpected {ch!r} in scalar")
        j += 1
    raw = text[i:j].strip()
    return parse_scalar(raw, lineno), j


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _dump_value(value: Any, indent: int) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _dump_string(value, indent)
    if isinstance(value, list):
        return _dump_list(value, indent)
    if isinstance(value, dict):
        return _dump_dict(value, indent)
    raise YAMLError(f"cannot dump type {type(value).__name__}")


def _has_special_controls(value: str) -> bool:
    for ch in value:
        code = ord(ch)
        if (code < 0x20 and ch not in "\n\t") or code == 0x7F or ch in "\x85\xa0\u2028\u2029":
            return True
    return False


def _dump_string(value: str, indent: int) -> str:
    if "\n" in value and _block_safe(value):
        clip = value.endswith("\n")
        body = value[:-1] if clip else value
        lines = body.split("\n")
        prefix = " " * indent
        out = ["|" if clip else "|-"]
        for line in lines:
            out.append(prefix + line if line != "" else "")
        return "\n".join(out)
    if "\n" in value or _needs_quotes(value):
        return '"' + _escape_double(value) + '"'
    return value


def _block_safe(value: str) -> bool:
    """True if a `|` block scalar reads back as exactly this string.

    Strings that fail this are written double-quoted with escapes.
    """
    if "\r" in value or value.endswith("\n\n") or _has_special_controls(value):
        return False
    lines = value.split("\n")
    if any(line != "" and line.strip() == "" for line in lines):
        return False
    first = next((line for line in lines if line != ""), "")
    # Leading spaces on the first line would be read as indentation.
    return first != "" and not first.startswith(" ")


def _needs_quotes(value: str) -> bool:
    if value == "":
        return True
    if value.strip() != value:
        return True
    if _has_special_controls(value):
        return True
    if value in ("true", "True", "TRUE", "false", "False", "FALSE", "null", "Null", "NULL", "~", "yes", "no", "on", "off"):
        return True
    if value[0] in "-?:{}[]!&*#|>%@`'\" ":
        return True
    if any(ch in value for ch in ":#\n\t,"):
        return True
    try:
        return parse_scalar(value) != value
    except YAMLError:
        return True


def _escape_double(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\0":
            out.append("\\0")
        else:
            code = ord(ch)
            if code < 0x20 or code == 0x7F or ch in "\x85\xa0\u2028\u2029":
                out.append(f"\\u{code:04x}")
            else:
                out.append(ch)
    return "".join(out)


def _dump_list(value: list[Any], indent: int) -> str:
    if not value:
        return "[]"
    parts: list[str] = []
    prefix = " " * indent
    for item in value:
        if isinstance(item, dict):
            dumped = _dump_dict(item, indent + 2)
            lines = dumped.split("\n")
            pad = " " * (indent + 2)
            first = lines[0]
            if first.startswith(pad):
                first = first[len(pad) :]
            else:
                first = first.lstrip()
            parts.append(f"{prefix}- {first}")
            for line in lines[1:]:
                parts.append(line)
        elif isinstance(item, list) and item:
            dumped = _dump_list(item, indent + 2)
            parts.append(f"{prefix}-")
            for line in dumped.split("\n"):
                parts.append(line)
        else:
            dumped = _dump_value(item, indent + 2)
            if "\n" in dumped:
                first, *rest = dumped.split("\n")
                parts.append(f"{prefix}- {first}")
                for line in rest:
                    parts.append(line)
            else:
                parts.append(f"{prefix}- {dumped}")
    return "\n".join(parts)


def _dump_dict(value: dict[str, Any], indent: int) -> str:
    if not value:
        return "{}"
    parts: list[str] = []
    prefix = " " * indent
    for key, item in value.items():
        key_text = _dump_key(key)
        if isinstance(item, (dict, list)) and item:
            dumped = _dump_value(item, indent + 2)
            parts.append(f"{prefix}{key_text}:")
            parts.append(dumped)
        elif isinstance(item, (dict, list)) and not item:
            dumped = _dump_value(item, indent + 2)
            parts.append(f"{prefix}{key_text}: {dumped}")
        else:
            dumped = _dump_value(item, indent + 2)
            if "\n" in dumped:
                first, *rest = dumped.split("\n")
                parts.append(f"{prefix}{key_text}: {first}")
                for line in rest:
                    parts.append(line)
            else:
                parts.append(f"{prefix}{key_text}: {dumped}")
    return "\n".join(parts)


def _dump_key(key: str) -> str:
    if not isinstance(key, str):
        key = str(key)
    if key == "<<" or _needs_quotes(key) or " " in key:
        return '"' + _escape_double(key) + '"'
    return key
