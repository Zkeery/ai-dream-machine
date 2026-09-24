# -*- coding: utf-8 -*-
"""文件与路径安全。"""
from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.core.path_security import check_ext, ensure_inside, safe_filename, validate_session_id


def test_safe_filename_ok():
    assert safe_filename("cat.png") == "cat.png"


@pytest.mark.parametrize("bad", ["../evil.png", "a/b.png", "", "名字 带 空格.png", "a" * 200])
def test_safe_filename_bad(bad):
    with pytest.raises(AppError):
        safe_filename(bad)


def test_check_ext_ok():
    assert check_ext("a.png", {".png", ".jpg"}) == ".png"


def test_check_ext_bad():
    with pytest.raises(AppError):
        check_ext("a.exe", {".png"})


def test_validate_session_id():
    validate_session_id("abc123")
    with pytest.raises(AppError):
        validate_session_id("../etc/passwd")


def test_ensure_inside(tmp_path):
    inside = tmp_path / "ok.png"
    assert ensure_inside(tmp_path, inside) == inside.resolve()
    outside = tmp_path.parent / "evil.png"
    with pytest.raises(AppError):
        ensure_inside(tmp_path, outside)
