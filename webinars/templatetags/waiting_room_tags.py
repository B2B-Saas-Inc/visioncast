from django import template

register = template.Library()


@register.filter
def lookup(mapping, key):
    """Dict lookup for keys containing dots (e.g. ``custom.role``)."""
    if isinstance(mapping, dict):
        return mapping.get(key)
    return None
