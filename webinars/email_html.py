"""Plain text to safe email HTML: escaped paragraphs, line breaks and linked https URLs.

Kept apart from the destination code so the mail backends can use it without importing the
delivery machinery (which imports them)."""
import html
import re

_URL_IN_TEXT = re.compile(r"https://[^\s<>\"']+")
_LINK_TRAIL = ".,;:!?)"


def to_html_fragment(text):
    """The body as HTML paragraphs: escaped text, ``<br>`` line breaks, auto-linked https URLs.

    For providers whose own template supplies the page around it (PostHog)."""
    parts = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        escaped = html.escape(paragraph, quote=True)
        linked = _URL_IN_TEXT.sub(_anchor, escaped)
        parts.append("<p>" + linked.replace("\n", "<br>\n") + "</p>")
    return "\n".join(parts)


def _anchor(match):
    url = match.group(0)
    trail = ""
    while url and url[-1] in _LINK_TRAIL:
        trail = url[-1] + trail
        url = url[:-1]
    return f'<a href="{url}">{url}</a>{trail}'
