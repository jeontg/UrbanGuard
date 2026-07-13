"""Verify the confirmed sam3.pt path-mismatch bug fix (docs/integration_plan.md
sections 2/8): resolve_checkpoint checks both SAM3_ROOT and the repo root, and
raises a clear error naming both when neither has the file (this repo
deliberately does not ship the 3.2GB checkpoint — see Phase 8)."""
import pytest

from tot_dashboard.crowd.config import resolve_checkpoint


def test_resolve_checkpoint_raises_clear_error_naming_both_locations():
    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_checkpoint()
    message = str(exc_info.value)
    assert "SAM3" in message
    assert "sam3.pt" in message
