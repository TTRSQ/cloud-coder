import subprocess

from cloud_coder_vm.busy_markers import active, busy_marker


def test_marker_lifecycle(tmp_path):
    assert active(tmp_path) == []
    with busy_marker("launch", tmp_path):
        assert active(tmp_path) == ["launch"]
    assert active(tmp_path) == []
    assert list(tmp_path.iterdir()) == []


def test_marker_of_dead_process_is_stale_and_removed(tmp_path):
    child = subprocess.Popen(["true"])
    child.wait()
    (tmp_path / f"install-{child.pid}").write_text("")
    assert active(tmp_path) == []
    assert not (tmp_path / f"install-{child.pid}").exists()


def test_missing_directory_means_nothing_in_progress(tmp_path):
    assert active(tmp_path / "nope") == []


def test_required_marker_fails_loudly_without_directory(tmp_path):
    import pytest

    with pytest.raises(OSError), busy_marker("launch", tmp_path / "nope"):
        pass
    with busy_marker("install", tmp_path / "nope", required=False):
        pass
