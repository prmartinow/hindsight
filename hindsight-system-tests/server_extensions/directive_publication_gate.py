"""Hold database publication after Git accepts a change, without modifying the server under test."""

import os
import subprocess
from pathlib import Path

from hindsight_api.extensions import OperationValidatorExtension, ValidationResult


class DirectivePublicationGate(OperationValidatorExtension):
    async def validate_retain(self, ctx):
        return ValidationResult.accept()

    async def validate_recall(self, ctx):
        return ValidationResult.accept()

    async def validate_reflect(self, ctx):
        return ValidationResult.accept()

    async def validate_bank_write(self, ctx):
        hold = Path(os.environ["DIRECTIVE_STORY_HOLD"])
        if hold.exists():
            head = subprocess.check_output(
                ["git", "--git-dir", str(hold.parent / "rules.git"), "rev-parse", "refs/heads/directives"], text=True
            ).strip()
            if head != hold.read_text():
                return ValidationResult.reject("Publication temporarily held", status_code=503)
        return ValidationResult.accept()
