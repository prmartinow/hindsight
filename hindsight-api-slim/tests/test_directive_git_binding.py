import json
from types import SimpleNamespace

import pytest

from hindsight_api.config import _parse_directive_git_repositories
from hindsight_api.engine.directive_git import service
from hindsight_api.schema_context import _current_schema


def test_same_bank_name_uses_only_authenticated_schema(tmp_path, monkeypatch):
    path = str(tmp_path / "one.git")
    bindings = _parse_directive_git_repositories(json.dumps({"tenant_one": {"shared": path}}))
    monkeypatch.setattr(service, "get_config", lambda: SimpleNamespace(directive_git_repositories=bindings))
    token = _current_schema.set("tenant_two")
    try:
        assert service.manager_for_bank("shared") is None
        _current_schema.set("tenant_one")
        assert service.manager_for_bank("shared").path == path
    finally:
        _current_schema.reset(token)


def test_duplicate_repository_through_symlink_is_rejected(tmp_path):
    real = tmp_path / "rules.git"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(ValueError, match="only one"):
        _parse_directive_git_repositories(json.dumps({"one": {"bank": str(real)}, "two": {"bank": str(alias)}}))
