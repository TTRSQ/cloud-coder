"""Which ignored paths of a worktree are caches that a build or a tool regenerates.

`close` deletes these with the worktree; any other ignored path (a .env, a database,
research output in out/, anything unknown) stops it. A cache is recognised by its name
and by what its tool leaves in it, so a directory that only borrows the name is not one:

- ``target/``: a Cargo target directory (cargo's CACHEDIR.TAG), holding only what cargo
  writes at its top two levels (profile directories such as debug/ with their deps/,
  incremental/, ..., dep-info files and the outputs cargo hard-links from deps/)
- ``.venv/``: a Python virtual environment (pyvenv.cfg), holding only what venv and uv
  create at its top level
- ``node_modules/``: next to a package.json
- ``__pycache__/`` with only .pyc files, and a single ``*.pyc`` file
- ``.pytest_cache/``, ``.ruff_cache/``, ``.mypy_cache/``: tagged with CACHEDIR.TAG

A symlink is never a cache (removing it is harmless, but it may be the only record of
where something lives). Inside a cache, symlinks are fine: removing one deletes the
link, not what it points to. ``dist/`` and ``build/`` are not caches here: nothing tells
a build's output from files put there by hand.
"""

import contextlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

# https://bford.info/cachedir/
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"

NOT_A_CACHE = "not a known regenerable cache"

_TARGET_FILES = {"CACHEDIR.TAG", ".rustc_info.json", ".rustdoc_fingerprint.json"}
# cargo doc, cargo package, CARGO_TARGET_TMPDIR of integration tests
_TARGET_DIRS = {"doc", "package", "tmp"}
_PROFILE_DIRS = {".fingerprint", "build", "deps", "examples", "incremental"}
_PROFILE_LOCKS = {".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock"}

_VENV_ENTRIES = {
    "pyvenv.cfg",
    "bin",
    "lib",
    "include",
    "share",
    "etc",
    "Scripts",
    "Lib",
    "Include",
    ".gitignore",
    "CACHEDIR.TAG",
    ".lock",
}


def _entries(path: Path) -> list[os.DirEntry]:
    with os.scandir(path) as it:
        return list(it)


def _is_dir(entry: os.DirEntry) -> bool:
    return entry.is_dir(follow_symlinks=False)


def _is_file(entry: os.DirEntry) -> bool:
    return entry.is_file(follow_symlinks=False)


def _is_file_path(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _tagged(path: Path) -> bool:
    tag = path / "CACHEDIR.TAG"
    if not _is_file_path(tag):
        return False
    with tag.open("rb") as f:
        return f.read(len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE


def _is_profile_dir(path: Path) -> bool:
    return (path / ".fingerprint").is_dir() or _is_file_path(path / ".cargo-lock")


def _inodes(path: Path) -> set[tuple[int, int]]:
    try:
        entries = _entries(path)
    except OSError:
        return set()
    inodes = set()
    for entry in entries:
        if _is_file(entry):
            inodes.add(_inode(entry))
    return inodes


def _profile_dir_problem(path: Path) -> str | None:
    """debug/, release/, ...: cargo's own directories and lock files, dep-info (.d)
    files, and outputs cargo "uplifts" from deps/ (hello, libhello.rlib), which are hard
    links to files in deps/."""
    in_deps = _inodes(path / "deps")
    for entry in _entries(path):
        if _is_dir(entry) and entry.name in _PROFILE_DIRS:
            continue
        if _is_file(entry) and (
            entry.name in _PROFILE_LOCKS or entry.name.endswith(".d") or _inode(entry) in in_deps
        ):
            continue
        return f"has {path.name}/{entry.name}, which cargo does not write there"
    return None


def _inode(entry: os.DirEntry) -> tuple[int, int]:
    st = entry.stat(follow_symlinks=False)
    return st.st_dev, st.st_ino


def _cargo_target(path: Path) -> str | None:
    if not _tagged(path):
        return "not a Cargo target directory (no CACHEDIR.TAG from cargo)"
    for entry in _entries(path):
        sub = Path(entry.path)
        if _is_file(entry) and entry.name in _TARGET_FILES:
            continue
        if _is_dir(entry) and entry.name in _TARGET_DIRS:
            continue
        if _is_dir(entry) and _is_profile_dir(sub):
            problem = _profile_dir_problem(sub)
            if problem is not None:
                return problem
            continue
        if _is_dir(entry) and _target_triple_problem(sub) is None:
            continue
        return f"has {entry.name}, which cargo does not write there"
    return None


def _target_triple_problem(path: Path) -> str | None:
    """target/<triple>/ of a cross build holds profile directories (and doc/)."""
    entries = _entries(path)
    if not entries:
        return "empty"
    for entry in entries:
        if _is_dir(entry) and entry.name == "doc":
            continue
        if _is_dir(entry) and _is_profile_dir(Path(entry.path)):
            problem = _profile_dir_problem(Path(entry.path))
            if problem is not None:
                return problem
            continue
        return "not a target triple directory"
    return None


def _venv(path: Path) -> str | None:
    if not _is_file_path(path / "pyvenv.cfg"):
        return "not a virtual environment (no pyvenv.cfg)"
    for entry in _entries(path):
        if entry.name == "lib64" and entry.is_symlink() and os.readlink(entry.path) == "lib":
            continue
        if entry.name in _VENV_ENTRIES and not entry.is_symlink():
            continue
        return f"has {entry.name}, which a virtual environment does not have"
    return None


def _node_modules(path: Path) -> str | None:
    if not _is_file_path(path.parent / "package.json"):
        return "no package.json next to it"
    return None


def _pycache(path: Path) -> str | None:
    for entry in _entries(path):
        if not (_is_file(entry) and entry.name.endswith(".pyc")):
            return f"has {entry.name}, which is not a .pyc file"
    return None


def _tool_cache(path: Path) -> str | None:
    return None if _tagged(path) else "no CACHEDIR.TAG"


_DIRECTORY_CACHES: dict[str, Callable[[Path], str | None]] = {
    "target": _cargo_target,
    ".venv": _venv,
    "node_modules": _node_modules,
    "__pycache__": _pycache,
    ".pytest_cache": _tool_cache,
    ".ruff_cache": _tool_cache,
    ".mypy_cache": _tool_cache,
}


def cache_root(relative: str) -> str:
    """The path to judge for an ignored path git listed: the outermost directory on it
    named like a cache. A cache that ignores itself (pytest, ruff, mypy and uv write a
    .gitignore of "*" in theirs) is listed by its content, .venv/pyvenv.cfg and so on,
    and is judged as the whole directory."""
    parts = relative.rstrip("/").split("/")
    for i, part in enumerate(parts[:-1]):
        if part in _DIRECTORY_CACHES:
            return "/".join(parts[: i + 1]) + "/"
    return relative


def why_not_a_cache(path: Path) -> str | None:
    """None when ``path`` is a regenerable cache; otherwise why it is not one."""
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            return "a symlink"
        if stat.S_ISREG(mode):
            return None if path.suffix == ".pyc" else NOT_A_CACHE
        check = _DIRECTORY_CACHES.get(path.name) if stat.S_ISDIR(mode) else None
        if check is None:
            return NOT_A_CACHE
        return check(path)
    except OSError as e:
        return f"cannot be read ({e.strerror})"


def disk_usage(path: Path) -> int:
    """Bytes on disk that deleting ``path`` would free: symlinks and mount points are not
    followed, and a file hard-linked from outside ``path`` (uv links .venv files to its
    cache) is not counted."""
    try:
        device = path.lstat().st_dev
    except OSError:
        return 0
    links: dict[tuple[int, int], list[int]] = {}  # inode -> [links seen, st_nlink, bytes]
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            st = current.lstat()
        except OSError:
            continue
        if st.st_dev != device:
            continue
        if stat.S_ISDIR(st.st_mode):
            total += st.st_blocks * 512
            with contextlib.suppress(OSError):
                stack.extend(Path(entry.path) for entry in _entries(current))
        elif st.st_nlink > 1:
            seen = links.setdefault((st.st_dev, st.st_ino), [0, st.st_nlink, st.st_blocks * 512])
            seen[0] += 1
        else:
            total += st.st_blocks * 512
    return total + sum(size for seen, nlink, size in links.values() if seen >= nlink)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value / 1024:.1f} GiB"
