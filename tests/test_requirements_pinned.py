"""The Docker image installs requirements.txt with pip --no-deps, so the file
must be the COMPLETE, exactly pinned set exported from uv.lock. A dependency
added to pyproject.toml without re-exporting would pass every test here (they
run on the uv venv) and fail only when the container imports it."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _name(spec: str) -> str:
    return re.split(r"[\[<>=!~; ]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def test_requirements_are_exact_pins_covering_every_dependency():
    lines = [ln.strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert lines
    for ln in lines:
        assert "==" in ln, f"not an exact pin: {ln!r} - re-export from uv.lock (see Dockerfile)"
    pinned = {_name(ln) for ln in lines}
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    missing = {_name(d) for d in deps} - pinned
    assert not missing, f"in pyproject.toml but not requirements.txt: {missing}"
