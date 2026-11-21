"""Validation for opt-in evidence scope on generated documents."""

from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..extensions.operation_validator import OperationValidationError
from .search.tags import TagGroup, TagGroupAnd, TagGroupLeaf, TagGroupNot

_STRICT_MATCHES = ("any_strict", "all_strict", "exact")
_TAG_GROUP = TypeAdapter(TagGroup)


def _bounds(group: TagGroup) -> tuple[bool, bool]:
    """Return whether the expression is exact and whether it is positively bounded."""
    if isinstance(group, TagGroupLeaf):
        exact = (
            group.resolve == "exact"
            and group.match in _STRICT_MATCHES
            and bool(group.tags)
            and all(tag.strip() for tag in group.tags)
        )
        return exact, exact
    if isinstance(group, TagGroupNot):
        exact, _ = _bounds(group.filter)
        return exact, False
    children = [_bounds(child) for child in group.filters]
    exact = bool(children) and all(valid for valid, _ in children)
    if isinstance(group, TagGroupAnd):
        return exact, any(bounded for _, bounded in children)
    return exact, all(bounded for _, bounded in children)


def validate_refresh_evidence_scope(tags: list[str] | None, trigger: dict[str, Any] | None) -> None:
    """Require an explicit, positive filter when scoped evidence is enabled.

    The creator's persisted access scope is intersected separately at refresh.
    """
    trigger = trigger or {}
    required = trigger.get("require_scoped_evidence")
    if required is None or required is False:
        return
    if required is not True:
        raise OperationValidationError("require_scoped_evidence must be a boolean", status_code=400)

    requested = trigger.get("tag_groups")
    if requested is None:
        match = trigger.get("tags_match") or "all_strict"
        valid = match in _STRICT_MATCHES and bool(tags) and all(tag.strip() for tag in tags)
    else:
        try:
            groups = [_TAG_GROUP.validate_python(group) for group in requested]
        except (TypeError, ValidationError) as error:
            raise OperationValidationError("Invalid tag_groups for scoped evidence", status_code=400) from error
        bounds = [_bounds(group) for group in groups]
        valid = all(exact for exact, _ in bounds) and any(bounded for _, bounded in bounds)

    if not valid:
        raise OperationValidationError(
            "Scoped evidence requires nonempty tags with a strict match mode "
            "or exact tag_groups with a positive filter in every OR branch.",
            status_code=400,
        )
