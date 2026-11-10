"""Focused app.js regressions using Node built-ins, without a browser or API."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for JavaScript UI regressions")
def test_async_ui_state_and_contract_validation():
    result = subprocess.run(
        [shutil.which("node"), str(Path(__file__).with_name("ui_state_regressions.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
