import subprocess
import sys


def test_cli_version():
    # Check that CLI runs and prints a version/help
    completed = subprocess.run(
        [sys.executable, '-m', 'goldcomb', '--help'],
        capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0
    assert ("goldcomb" in completed.stdout.lower() or "usage" in completed.stdout.lower())
    assert "--sudo" in completed.stdout
