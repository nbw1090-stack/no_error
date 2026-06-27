"""extractor.extract_logs 单元测试 —— 内存 tar.gz 提取"""

import tarfile

import pytest

from extractor import extract_logs


def test_extract_both_files(make_tar_gz):
    data = make_tar_gz(app_text="APP-CONTENT", framework_text="FW-CONTENT")
    app, fw = extract_logs(data)
    assert app == "APP-CONTENT"
    assert fw == "FW-CONTENT"


def test_missing_app_returns_empty(make_tar_gz):
    data = make_tar_gz(app_text=None, framework_text="FW")
    app, fw = extract_logs(data)
    assert app == ""
    assert fw == "FW"


def test_missing_framework_returns_empty(make_tar_gz):
    data = make_tar_gz(app_text="APP", framework_text=None)
    app, fw = extract_logs(data)
    assert app == "APP"
    assert fw == ""


def test_empty_tar(make_tar_gz):
    data = make_tar_gz(app_text=None, framework_text=None)
    assert extract_logs(data) == ("", "")


def test_backslash_path_separator(make_tar_gz):
    # Windows 风格反斜杠路径仍应命中（extractor 做 replace('\\','/')）
    data = make_tar_gz(
        app_text="A", framework_text="F",
        app_name=r"Dump\LogDump\app.log",
        framework_name=r"Dump\LogDump\framework.log",
    )
    app, fw = extract_logs(data)
    assert app == "A"
    assert fw == "F"


def test_nested_prefix_matched(make_tar_gz):
    data = make_tar_gz(
        app_text="A", framework_text="F",
        app_name="a/b/c/LogDump/app.log",
        framework_name="a/b/c/LogDump/framework.log",
    )
    app, fw = extract_logs(data)
    assert app == "A" and fw == "F"


def test_directory_members_skipped(make_tar_gz):
    # 多余的目录成员不应干扰提取
    data = make_tar_gz(
        app_text="A", framework_text="F",
        extra_members=[("Dump/LogDump", None)],  # 目录成员
    )
    app, fw = extract_logs(data)
    assert app == "A" and fw == "F"


def test_invalid_gzip_raises():
    with pytest.raises(tarfile.ReadError):
        extract_logs(b"this is not a tar.gz at all")


def test_utf8_decode_errors_ignored(make_tar_gz):
    # 嵌入非法 utf8 字节，errors='ignore' 不应抛异常
    bad_bytes = b"ok\xff\xfe text"
    buf = __import__("io").BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="Dump/LogDump/app.log")
        info.size = len(bad_bytes)
        tar.addfile(info, __import__("io").BytesIO(bad_bytes))
    app, _ = extract_logs(buf.getvalue())
    assert "ok" in app  # 非法字节被忽略，合法部分保留
