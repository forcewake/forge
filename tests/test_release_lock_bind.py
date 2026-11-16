"""A release lock may gain only its exact wheel lines."""

import pytest

from tools.release.artifacts import bind_lock

WHEEL = (
    "forge-protocol @ https://github.com/forcewake/forge/releases/download/"
    "v1.0.0-rc.1/forge_protocol-0.1.0-py3-none-any.whl --hash=sha256:" + "b" * 64
)


def test_bind_lock_appends_only_the_named_wheel_lines(tmp_path):
    path = tmp_path / "lane-v1-requirements.txt"
    locked = "annotated-types==0.8.0 --hash=sha256:" + "a" * 64 + "\n"
    bind_lock(path, locked, (WHEEL,))
    assert path.read_text() == locked + WHEEL + "\n"
    bind_lock(path, locked, (WHEEL,))
    assert path.read_text().count(WHEEL) == 1


def test_bind_lock_rejects_an_extra_or_unrelated_line(tmp_path):
    path = tmp_path / "lane-v1-requirements.txt"
    locked = "annotated-types==0.8.0 --hash=sha256:" + "a" * 64 + "\n"
    bind_lock(path, locked, (WHEEL,))
    extra = (
        "forge-lane @ https://github.com/forcewake/forge/releases/download/"
        "v1.0.0-rc.1/forge_lane-0.1.0-py3-none-any.whl --hash=sha256:" + "c" * 64
    )
    with pytest.raises(ValueError, match="previously bound"):
        bind_lock(path, locked, (WHEEL, extra))
    with pytest.raises(ValueError, match="single-line"):
        bind_lock(path, locked, ("unexpected==1",))
    assert path.read_text() == locked + WHEEL + "\n"
