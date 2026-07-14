from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_editable_installs_use_locked_build_tooling():
    lock_lines = (PROJECT_ROOT / "requirements.lock").read_text().splitlines()

    assert [line for line in lock_lines if line.startswith("setuptools")] == [
        "setuptools==80.9.0"
    ]
    assert [line for line in lock_lines if line.startswith("wheel")] == [
        "wheel==0.45.1"
    ]

    setup_lines = (PROJECT_ROOT / "scripts" / "setup.ps1").read_text().splitlines()
    editable_installs = [
        line for line in setup_lines if "pip install" in line and "--editable" in line
    ]

    assert len(editable_installs) == 2
    for command in editable_installs:
        assert "--no-build-isolation" in command
        assert "--no-deps" in command
