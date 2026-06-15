"""Small HTML sanitizer for job descriptions rendered inside the local app."""
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit


ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "s", "span", "strong",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
VOID_TAGS = {"br", "hr"}
DROP_CONTENT_TAGS = {
    "base", "button", "embed", "form", "iframe", "input", "link", "math", "meta",
    "object", "script", "select", "style", "svg", "textarea",
}
GLOBAL_ATTRS = {"title"}
TAG_ATTRS = {
    "a": {"href", "title"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}
SAFE_LINK_SCHEMES = {"http", "https", "mailto"}


def _safe_href(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in SAFE_LINK_SCHEMES:
        return ""
    return value


def _safe_span(value: str) -> str:
    value = (value or "").strip()
    if not value.isdigit():
        return ""
    number = max(1, min(int(value), 12))
    return str(number)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.drop_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self.drop_depth += 1
            return
        if self.drop_depth or tag not in ALLOWED_TAGS:
            return
        safe_attrs = []
        allowed_attrs = TAG_ATTRS.get(tag, set()) | GLOBAL_ATTRS
        for name, value in attrs:
            name = (name or "").lower()
            value = value or ""
            if name not in allowed_attrs:
                continue
            if name == "href":
                value = _safe_href(value)
                if not value:
                    continue
            elif name in {"colspan", "rowspan"}:
                value = _safe_span(value)
                if not value:
                    continue
            safe_attrs.append(f' {name}="{escape(value, quote=True)}"')
        if tag == "a" and any(attr.startswith(" href=") for attr in safe_attrs):
            safe_attrs.append(' target="_blank" rel="noopener noreferrer"')
        self.parts.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_CONTENT_TAGS:
            self.drop_depth = max(0, self.drop_depth - 1)
            return
        if self.drop_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not self.drop_depth:
            self.parts.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.drop_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.drop_depth:
            self.parts.append(f"&#{name};")


def sanitize_html(value: str | None) -> str:
    parser = _Sanitizer()
    parser.feed(value or "")
    parser.close()
    return "".join(parser.parts)
