"""Small text helpers shared by the scrapers.

`html_to_text()` turns the HTML job descriptions some boards return (Workable,
Workday) into plain text, so the dashboard and the cover-letter prompt do not
get raw markup. Standard library only (html.parser); no new dependency.
"""

import html
import re
from html.parser import HTMLParser
from typing import List, Optional

# Content of these tags is never text a reader sees.
_SKIPPED = {"script", "style"}
# Tags that start and end a paragraph (a blank line around them).
_PARAGRAPH = {
    "p", "div", "section", "article", "blockquote", "table",
    "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
}
# Tags that start and end a single line.
_LINE = {"br", "tr", "li"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._newlines = 1  # trailing newlines already emitted (1 = at line start)
        self._skip_depth = 0

    def _break(self, count: int) -> None:
        """Make sure the output ends with at least `count` newlines."""
        missing = count - self._newlines
        if missing > 0:
            self._parts.append("\n" * missing)
            self._newlines = count

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIPPED:
            self._skip_depth += 1
        elif tag in _PARAGRAPH:
            self._break(2)
        elif tag in _LINE:
            self._break(1)
            if tag == "li":
                self._parts.append("- ")
                self._newlines = 0

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _PARAGRAPH:
            self._break(2)
        elif tag in _LINE:
            self._break(1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data)  # HTML collapses whitespace (incl. nbsp)
        if self._newlines:
            text = text.lstrip()
        if not text:
            return
        self._parts.append(text)
        self._newlines = 0

    def text(self) -> str:
        lines = [line.rstrip() for line in "".join(self._parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(value: Optional[str]) -> str:
    """HTML -> readable plain text. Never raises; None or "" gives "".

    Block tags become line breaks (paragraphs get a blank line), list items
    start with "- ", script and style content is dropped, entities are decoded
    and runs of blank lines collapse. Text without any tags passes through
    unchanged apart from whitespace tidying.
    """
    if not value:
        return ""
    try:
        parser = _TextExtractor()
        parser.feed(str(value))
        parser.close()
        return parser.text()
    except Exception:  # noqa: BLE001 - a description is not worth failing a scrape
        # Crude fallback: drop anything that looks like a tag, decode entities.
        stripped = re.sub(r"<[^>]*>", " ", str(value))
        return re.sub(r"\s+", " ", html.unescape(stripped)).strip()
