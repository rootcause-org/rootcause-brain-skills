from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lib import fs


def test_mirror_path_precedence_and_missing_is_authoritative(tmp_path: Path, monkeypatch) -> None:
    farm = tmp_path / "farm"
    fallback = farm / "demo"
    explicit = tmp_path / "explicit"
    fallback.mkdir(parents=True)
    explicit.mkdir()
    monkeypatch.setenv("RC_MIRRORS_ROOT", str(farm))
    monkeypatch.setenv("RC_MIRROR_DEMO", str(explicit))

    assert fs.mirror_path("demo") == explicit.resolve()

    missing = tmp_path / "missing"
    monkeypatch.setenv("RC_MIRROR_DEMO", str(missing))
    with pytest.raises(fs.MirrorMissing, match="candidates") as exc:
        fs.mirror_path("demo")
    assert str(missing) in str(exc.value)
    assert str(fallback) in str(exc.value)


def test_mirror_scripts_prepends_once(tmp_path: Path, monkeypatch) -> None:
    scripts = tmp_path / "mirror" / "skills" / "records" / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setenv("RC_MIRROR_DEMO", str(tmp_path / "mirror"))
    monkeypatch.setattr(sys, "path", ["existing"])

    assert fs.mirror_scripts("demo", "skills/records/scripts") == scripts.resolve()
    fs.mirror_scripts("demo", "skills/records/scripts")
    assert sys.path == [str(scripts.resolve()), "existing"]


def test_mirror_name_must_be_docker_safe() -> None:
    with pytest.raises(ValueError, match="Docker-safe repository slug"):
        fs.mirror_path("foo:rw")
