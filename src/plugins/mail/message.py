"""Reading a fetched mail: headers, the text body (HTML turned into text), the attachments, safe file names."""
from __future__ import annotations

import re
import unicodedata
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

_BLOCK = {"br", "p", "div", "li", "tr", "table", "ul", "ol", "blockquote", "pre", "hr", "section", "article",
          "h1", "h2", "h3", "h4", "h5", "h6"}
_HIDDEN = {"script", "style"}
_WINDOWS_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com\d|lpt\d)(\.|$)")
FILENAME_MAX = 150
NESTING_MAX = 50        # real mails nest a few levels; ~1000 make the stdlib's recursive parser raise
UNREADABLE = "(This mail could not be read: its parts nest too deep or are malformed.)"


def _depth(message: EmailMessage) -> int:
    deepest, stack = 0, [(message, 1)]
    while stack:
        part, depth = stack.pop()
        deepest = max(deepest, depth)
        if part.is_multipart():          # an attached mail (message/rfc822) counts too: as_bytes recurses into it
            stack.extend((child, depth + 1) for child in part.get_payload())
    return deepest


def parse(raw: bytes) -> EmailMessage:
    """The mail; one nested too deep (or that the parser chokes on) is its headers and a text that says so --
    one such mail from anyone must not break listing the mailbox."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw or b"")
        if _depth(message) <= NESTING_MAX:
            return message
    except Exception:            # RecursionError
        pass
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw or b"", headersonly=True)
        for name in ("Content-Type", "Content-Transfer-Encoding", "Content-Disposition"):
            del message[name]
    except Exception:            # the top-level headers themselves: no header at all
        message = EmailMessage()
    message.set_payload(UNREADABLE)
    return message


def header(message: EmailMessage, name: str) -> str:
    """One header as one line; "" when it is missing or too broken to decode."""
    try:
        value = message.get(name)
    except Exception:            # a malformed header the policy cannot take
        value = None
    return " ".join(str(value).split()) if value is not None else ""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.href = ""

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            self.hidden += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):              # cells side by side: "Total 120.00", never "Total120.00"
            self.parts.append(" ")
        elif tag == "a":
            self.href = dict(attrs).get("href") or ""

    def handle_endtag(self, tag):
        if tag in _HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")
        elif tag == "a" and self.href:
            self.parts.append(f" <{self.href}>")       # where a link really goes is part of what a mail says
            self.href = ""

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _content(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except Exception:            # an unknown charset: the bytes as UTF-8, broken characters replaced
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def body_part(message: EmailMessage):
    return message.get_body(preferencelist=("plain", "html"))


def body_text(message: EmailMessage) -> str:
    """The text a person reads: text/plain where the mail has it, else its HTML as text."""
    part = body_part(message)
    if part is None:
        return ""
    text = _content(part)
    return html_to_text(text) if part.get_content_subtype() == "html" else text.strip()


def _leaves(part: EmailMessage):
    """The parts that are no container; an attached mail (message/rfc822) is one part, not opened."""
    if part.get_content_type() == "message/rfc822" or not part.is_multipart():
        yield part
        return
    for child in part.iter_parts():
        yield from _leaves(child)


def attachments(message: EmailMessage) -> list[EmailMessage]:
    """Every part that is not the body text: declared attachments, named parts, anything that is not text."""
    body = body_part(message)
    found = []
    for part in _leaves(message):
        if part is body:
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename() \
                or part.get_content_maintype() != "text":
            found.append(part)
    return found


def payload(part: EmailMessage) -> bytes:
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        return inner[0].as_bytes() if inner else b""
    return part.get_payload(decode=True) or b""


def filename(part: EmailMessage) -> str:
    try:
        return part.get_filename() or ""
    except Exception:
        return ""


def safe_filename(name: str, index: int) -> str:
    """A file name the sender cannot turn into a path: the last path part only, no control or reserved
    characters, no leading dots, no Windows device name, at most FILENAME_MAX characters (the extension kept)."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.split(r"[\\/]", name)[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*')
    name = name.strip(" .")
    if _WINDOWS_RESERVED.match(name):
        name = "_" + name
    if len(name) > FILENAME_MAX:
        stem, dot, ext = name.rpartition(".")
        ext = ext[:20] if dot and stem else ""
        name = name[:FILENAME_MAX - len(ext) - 1].rstrip(" .") + ("." + ext if ext else "")
    return name or f"attachment-{index}"
