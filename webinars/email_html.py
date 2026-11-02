"""Plain text to safe email HTML: escaped paragraphs, line breaks and linked https URLs.

Kept apart from the destination code so the mail backends can use it without importing the
delivery machinery (which imports them)."""
import html
import re

_URL_IN_TEXT = re.compile(r"https://[^\s<>\"']+")
_LINK_TRAIL = ".,;:!?)"


# PostHog's template layout zeroes paragraph margins, so its paragraphs carry their own spacing.
SPACED_PARAGRAPH = "margin:0 0 16px 0"


def to_html_fragment(text, paragraph_style=""):
    """The body as HTML paragraphs: escaped text, ``<br>`` line breaks, auto-linked https URLs.

    For providers whose own template supplies the page around it (PostHog). ``paragraph_style``
    is a fixed inline style from this module, never anything from a message."""
    opening = f'<p style="{paragraph_style}">' if paragraph_style else "<p>"
    parts = []
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        escaped = html.escape(paragraph, quote=True)
        linked = _URL_IN_TEXT.sub(_anchor, escaped)
        parts.append(opening + linked.replace("\n", "<br>\n") + "</p>")
    return "\n".join(parts)


def _anchor(match):
    url = match.group(0)
    trail = ""
    while url and url[-1] in _LINK_TRAIL:
        trail = url[-1] + trail
        url = url[:-1]
    return f'<a href="{url}">{url}</a>{trail}'
