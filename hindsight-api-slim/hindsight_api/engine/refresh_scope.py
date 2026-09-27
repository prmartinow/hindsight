"""Fail-closed evidence boundaries for generated knowledge documents.

A query describes a topic; only positive exact tags bound its sources. Negative
filters alone and OR branches without positive scope cannot establish isolation.
"""

from .search.tags import TagGroupAnd, TagGroupLeaf, TagGroupNot, TagGroupOr


def strict_match(match):
    return {"any": "any_strict", "all": "all_strict"}.get(match, match)


def strict_group(group):
    """Return (normalized group, positively bounded, valid exact expression)."""
    if isinstance(group, TagGroupLeaf):
        valid = bool(group.tags) and all(tag.strip() for tag in group.tags) and group.resolve == "exact"
        return group.model_copy(update={"match": strict_match(group.match)}), valid, valid
    if isinstance(group, TagGroupNot):
        child, _, valid = strict_group(group.filter)
        return group.model_copy(update={"filter": child}), False, valid
    children = [strict_group(child) for child in group.filters]
    valid = bool(children) and all(child[2] for child in children)
    if isinstance(group, TagGroupAnd):
        bounded = any(child[1] for child in children)
    elif isinstance(group, TagGroupOr):
        bounded = bool(children) and all(child[1] for child in children)
    else:
        return group, False, False
    return group.model_copy(update={"filters": [child[0] for child in children]}), bounded, valid
