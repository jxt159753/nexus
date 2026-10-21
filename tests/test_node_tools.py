import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus.contracts.base import MAX_RESULT_BYTES
from nexus.contracts.task import ListDirectoryArguments
from nexus.node.config import NodeConfig, local_directory
from nexus.node.tools.list_directory import list_directory
from nexus.node.tools.registry import REGISTRY, execute_tool
from nexus.node.tools.system_info import system_info
from node_support import command, configuration


def test_fixed_registry_and_field_whitelist(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("local tools must not call a model or network")
    monkeypatch.setattr("socket.socket", no_network)
    monkeypatch.setattr("socket.create_connection", no_network)
    assert set(REGISTRY) == {"system.info@1", "filesystem.list@1", "filesystem.read@1", "filesystem.search@1", "filesystem.write_preview@1", "filesystem.write@1", "command.run@1", "filesystem.read_lines@1", "filesystem.glob@1", "filesystem.grep@1", "filesystem.edit_preview@1", "filesystem.edit@1", "interaction.observe@1", "interaction.act@1"}
    assert all(t.read_only and t.retry_safe for key,t in REGISTRY.items() if key not in {"filesystem.write@1","filesystem.edit@1","command.run@1","interaction.act@1"})
    assert all(not REGISTRY[key].retry_safe for key in {"filesystem.write@1","filesystem.edit@1","command.run@1","interaction.act@1"})
    with pytest.raises(TypeError):
        REGISTRY["shell@1"] = None
    assert set(system_info().model_dump()) == {"os_name", "os_version", "architecture", "python_version"}
    cfg = NodeConfig(node_id=configuration(tmp_path).node_id, data_dir=tmp_path)
    assert not cfg.roots and not cfg.allowed_tools
    with pytest.raises(PermissionError):
        execute_tool(command(cfg), cfg)


@pytest.mark.parametrize("path", ["relative", "C:relative", "\\\\server\\share", "\\\\?\\C:\\Windows", "C:/Windows/*", "C:/Windows/../Windows"])
def test_non_local_or_non_literal_paths_rejected(path):
    with pytest.raises((ValueError, OSError)):
        local_directory(Path(path))


@pytest.mark.parametrize("changes", [
    {"allowed_tools": frozenset({"shell@1"})}, {"roots": {"other": Path("C:/")}},
    {"max_result_bytes": MAX_RESULT_BYTES + 1}, {"max_result_bytes": True},
    {"execution_timeout_seconds": 11}, {"max_receipts": 10001},
])
def test_config_limits(tmp_path, changes):
    with pytest.raises(ValidationError):
        configuration(tmp_path, **changes)


def junction(target, link):
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


def test_junction_root_ancestor_and_changed_root_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "nested").mkdir()
    link = tmp_path / "link"
    junction(real, link)
    try:
        for root in (link, link / "nested"):
            with pytest.raises(ValueError, match="reparse"):
                configuration(tmp_path, roots={"workspace": root})
        cfg = configuration(tmp_path, roots={"workspace": real})
        real.rename(tmp_path / "old")
        junction(tmp_path / "old", real)
        try:
            with pytest.raises(ValueError, match="reparse"):
                execute_tool(command(cfg, directory=True), cfg)
        finally:
            real.rmdir()
    finally:
        link.rmdir()


def test_listing_direct_entries_and_junction_not_followed(tmp_path):
    root, private = tmp_path / "root", tmp_path / "private"
    root.mkdir()
    private.mkdir()
    (private / "secret.txt").write_text("must never read")
    (root / "file.txt").write_text("content must not appear")
    (root / "directory").mkdir()
    link = root / "jump"
    junction(private, link)
    try:
        cfg = configuration(tmp_path, roots={"workspace": root})
        result = execute_tool(command(cfg, directory=True), cfg)
        assert {e.name: e.kind for e in result.entries} == {"file.txt": "file", "directory": "directory", "jump": "link"}
        assert result.truncated is False
        assert "secret" not in result.model_dump_json()
        assert str(tmp_path) not in result.model_dump_json()
        assert "content" not in result.model_dump_json()
    finally:
        link.rmdir()


def test_link_metadata_never_follows_target(tmp_path, monkeypatch):
    class Item:
        name = "link"
        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            return os.stat_result((stat.S_IFLNK, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    class Scan:
        def __enter__(self):
            return iter([Item()])
        def __exit__(self, *_):
            pass
    monkeypatch.setattr(os, "scandir", lambda _: Scan())
    result = list_directory(ListDirectoryArguments(root_id="workspace", limit=1), tmp_path, MAX_RESULT_BYTES)
    assert result.entries[0].kind == "link"


def test_count_truncation_and_exact_limit(tmp_path):
    for index in range(3):
        (tmp_path / f"file{index}").touch()
    assert list_directory(ListDirectoryArguments(root_id="workspace", limit=3), tmp_path, MAX_RESULT_BYTES).truncated is False
    result = list_directory(ListDirectoryArguments(root_id="workspace", limit=2), tmp_path, MAX_RESULT_BYTES)
    assert result.truncated is True and len(result.entries) == 2


def test_utf8_bytes_truncate_without_scanning_whole_directory(tmp_path):
    for index in range(100):
        (tmp_path / (str(index) + "中" * 110)).touch()
    args = ListDirectoryArguments(root_id="workspace", limit=200)
    for budget in (128, 1000, MAX_RESULT_BYTES):
        result = list_directory(args, tmp_path, budget)
        assert result.truncated is True
        assert len(result.model_dump_json().encode("utf-8")) <= budget
        assert len(result.entries) < 100


def test_missing_resource_and_non_directory_are_rejected(tmp_path):
    cfg = configuration(tmp_path)
    with pytest.raises(PermissionError):
        execute_tool(command(cfg, directory=True), cfg)
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(ValueError):
        configuration(tmp_path, roots={"workspace": file})
