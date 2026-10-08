import os
import shutil
import subprocess
import sys

import pytest

from cloud_coder_vm.regenerable_caches import (
    CACHEDIR_SIGNATURE,
    NOT_A_CACHE,
    cache_root,
    disk_usage,
    human_size,
    why_not_a_cache,
)

TAG = CACHEDIR_SIGNATURE + b"\n# created by a tool\n"


def cargo_target(root):
    """The layout cargo 1.99 leaves after build, test --no-run and doc."""
    target = root / "target"
    debug = target / "debug"
    for sub in [".fingerprint/hello-1", "build", "deps", "examples", "incremental/hello-2"]:
        (debug / sub).mkdir(parents=True)
    for name in [".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock", "hello.d"]:
        (debug / name).write_text("")
    (debug / "deps" / "hello-0123").write_bytes(b"\x7fELF")
    os.link(debug / "deps" / "hello-0123", debug / "hello")
    (debug / "deps" / "libhello-0123.rlib").write_bytes(b"rlib")
    os.link(debug / "deps" / "libhello-0123.rlib", debug / "libhello.rlib")
    (debug / "deps" / "tool-4567").write_bytes(b"\x7fELF")
    os.link(debug / "deps" / "tool-4567", debug / "tool")  # for a test: no tool.d
    (target / "doc" / "hello").mkdir(parents=True)
    (target / "tmp").mkdir()
    (target / "CACHEDIR.TAG").write_bytes(TAG)
    (target / ".rustc_info.json").write_text("{}")
    return target


def test_a_cargo_target_directory_is_a_cache(tmp_path):
    target = cargo_target(tmp_path)
    (target / "release" / ".fingerprint").mkdir(parents=True)
    (target / "x86_64-unknown-linux-gnu" / "debug" / ".fingerprint").mkdir(parents=True)
    assert why_not_a_cache(target) is None


@pytest.mark.skipif(shutil.which("cargo") is None, reason="needs cargo")
def test_what_cargo_really_writes_is_a_cache(tmp_path):
    crate = tmp_path / "hello"
    run = {"cwd": crate, "check": True, "capture_output": True}
    subprocess.run(["cargo", "init", "-q", "--offline", "--name", "hello", str(crate)], check=True)
    (crate / "tests").mkdir()
    (crate / "tests" / "it.rs").write_text("#[test]\nfn t() {}\n")
    # a binary integration tests run (CARGO_BIN_EXE_*) is uplifted without dep-info
    (crate / "tests" / "bin.rs").write_text('#[test]\nfn b() { env!("CARGO_BIN_EXE_hello"); }\n')
    subprocess.run(["cargo", "test", "-q", "--offline", "-j", "2", "--no-run"], **run)
    subprocess.run(["cargo", "build", "-q", "--offline", "-j", "2", "--release"], **run)
    assert why_not_a_cache(crate / "target") is None


@pytest.mark.parametrize(
    ("unknown", "reason"),
    [
        ("results.csv", "has results.csv"),  # put next to cargo's output by hand
        ("debug/notes.txt", "has debug/notes.txt"),
        ("debug/hello.json", "has debug/hello.json"),  # what the hello binary wrote
        ("debug/hello-copy", "has debug/hello-copy"),  # a binary, but not one from deps/
        ("data/x.parquet", "has data"),
        ("debug/out/x", "has debug/out"),
    ],
)
def test_a_target_directory_with_something_cargo_does_not_write_is_not(tmp_path, unknown, reason):
    target = cargo_target(tmp_path)
    (target / unknown).parent.mkdir(parents=True, exist_ok=True)
    (target / unknown).write_text("results")
    assert reason in why_not_a_cache(target)


def test_a_target_directory_cargo_did_not_tag_is_not(tmp_path):
    target = tmp_path / "target"  # Maven's, or one made by hand
    (target / "classes").mkdir(parents=True)
    assert "not a Cargo target directory" in why_not_a_cache(target)
    (target / "CACHEDIR.TAG").write_text("Signature: something else\n")
    assert "not a Cargo target directory" in why_not_a_cache(target)


def test_a_symlink_is_never_a_cache(tmp_path):
    outside = cargo_target(tmp_path / "shared")
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / "target").symlink_to(outside)
    assert why_not_a_cache(tmp_path / "wt" / "target") == "a symlink"
    (tmp_path / "wt" / "x.pyc").symlink_to(tmp_path / "shared")
    assert why_not_a_cache(tmp_path / "wt" / "x.pyc") == "a symlink"


def test_a_symlink_inside_cargo_s_levels_is_not_allowed(tmp_path):
    target = cargo_target(tmp_path)
    (target / "debug" / "data").symlink_to(tmp_path)
    assert "has debug/data" in why_not_a_cache(target)


def test_a_virtual_environment_is_a_cache(tmp_path):
    venv = tmp_path / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    assert why_not_a_cache(venv) is None
    (venv / "data.sqlite").write_text("")
    assert "has data.sqlite" in why_not_a_cache(venv)


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_a_uv_virtual_environment_is_a_cache(tmp_path):
    subprocess.run(["uv", "venv", "-q", str(tmp_path / ".venv")], check=True)
    assert why_not_a_cache(tmp_path / ".venv") is None


def test_a_venv_directory_without_pyvenv_cfg_is_not(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    assert "no pyvenv.cfg" in why_not_a_cache(tmp_path / ".venv")


def test_node_modules_needs_a_package_json_next_to_it(tmp_path):
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin").mkdir()
    (tmp_path / "node_modules" / ".bin" / "x").symlink_to("../left-pad/x")
    assert "package.json" in why_not_a_cache(tmp_path / "node_modules")
    (tmp_path / "package.json").write_text("{}")
    assert why_not_a_cache(tmp_path / "node_modules") is None


def test_pycache_holds_only_pyc_files(tmp_path):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "m.cpython-312.pyc").write_bytes(b"")
    assert why_not_a_cache(cache) is None
    assert why_not_a_cache(cache / "m.cpython-312.pyc") is None
    (cache / "notes.txt").write_text("")
    assert "notes.txt" in why_not_a_cache(cache)


@pytest.mark.parametrize("name", [".pytest_cache", ".ruff_cache", ".mypy_cache"])
def test_tool_caches_are_tagged(tmp_path, name):
    (tmp_path / name).mkdir()
    assert why_not_a_cache(tmp_path / name) == "no CACHEDIR.TAG"
    (tmp_path / name / "CACHEDIR.TAG").write_bytes(TAG)
    assert why_not_a_cache(tmp_path / name) is None


@pytest.mark.parametrize("name", [".env", "data.sqlite", "out", "dist", "build", "notes.txt"])
def test_anything_else_is_not_a_cache(tmp_path, name):
    if name in ("out", "dist", "build"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "CACHEDIR.TAG").write_bytes(TAG)  # a tag alone is not enough
    else:
        (tmp_path / name).write_text("")
    assert why_not_a_cache(tmp_path / name) == NOT_A_CACHE


def test_disk_usage_counts_what_deleting_frees(tmp_path):
    cache = tmp_path / "cache"
    (cache / "sub").mkdir(parents=True)
    (cache / "a").write_bytes(b"x" * 100_000)
    os.link(cache / "a", cache / "sub" / "a-again")  # both links go: freed
    (tmp_path / "shared").write_bytes(b"y" * 100_000)
    os.link(tmp_path / "shared", cache / "shared")  # still linked from outside: not freed
    (tmp_path / "big").write_bytes(b"z" * 1_000_000)
    (cache / "link").symlink_to(tmp_path / "big")  # not followed
    alone = disk_usage(tmp_path / "shared")
    usage = disk_usage(cache)
    a_size = (cache / "a").stat().st_blocks * 512
    assert a_size <= usage < a_size + 100_000
    assert alone == 0  # its other link is in cache/


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(1536) == "1.5 KiB"
    assert human_size(3 * 1024**3) == "3.0 GiB"


@pytest.mark.parametrize(
    ("listed", "root"),
    [
        (".venv/pyvenv.cfg", ".venv/"),
        (".pytest_cache/v/", ".pytest_cache/"),
        ("web/node_modules/x/target/a", "web/node_modules/"),
        ("crates/a/target/", "crates/a/target/"),
        ("out/", "out/"),
        (".env", ".env"),
    ],
)
def test_cache_root(listed, root):
    assert cache_root(listed) == root
