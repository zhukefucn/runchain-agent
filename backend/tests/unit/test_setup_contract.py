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


def test_database_dependencies_are_exactly_pinned():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    lock_lines = (PROJECT_ROOT / "requirements.lock").read_text().splitlines()
    required_pins = [
        "SQLAlchemy==2.0.44",
        "aiosqlite==0.21.0",
        "argon2-cffi==25.1.0",
    ]

    for pin in required_pins:
        assert f'"{pin}"' in pyproject
        assert pin in lock_lines


def test_auth_api_dependencies_are_exactly_pinned():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    lock_lines = (PROJECT_ROOT / "requirements.lock").read_text().splitlines()
    runtime_pins = [
        "fastapi==0.139.0",
        "PyJWT==2.13.0",
    ]
    test_pins = ["httpx==0.28.1"]

    for pin in runtime_pins:
        assert f'"{pin}"' in pyproject
        assert pin in lock_lines
    for pin in test_pins:
        assert f'"{pin}"' in pyproject
        assert pin in lock_lines
    assert "starlette==1.3.1" in lock_lines


def test_agentscope_service_dependencies_are_exactly_pinned():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    lock_lines = (PROJECT_ROOT / "requirements.lock").read_text().splitlines()
    required_pins = [
        "APScheduler==3.11.3",
        "ag-ui-protocol==0.1.19",
        "uvicorn==0.51.0",
    ]

    for pin in required_pins:
        assert f'"{pin}"' in pyproject
        assert pin in lock_lines


def test_skill_manifest_schema_validator_is_exactly_pinned():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    lock_lines = (PROJECT_ROOT / "requirements.lock").read_text().splitlines()
    assert '"jsonschema==4.26.0"' in pyproject
    assert "jsonschema==4.26.0" in lock_lines


def test_mcp_sdk_is_a_direct_exact_runtime_dependency():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    lock_lines = (PROJECT_ROOT / "requirements.lock").read_text().splitlines()
    assert '"mcp==1.28.1"' in pyproject
    assert "mcp==1.28.1" in lock_lines
