from pathlib import Path

import pytest

from cloud_coder_vm.launch import LaunchError, Layout, claude_command, resolve_target
from cloud_coder_vm.session_registry import LogicalSession

WS = Path("/home/coder/git")
LAYOUT = Layout(WS, WS / "wt", Path("/home/coder/workspace"))


def entry(name, repo, t, url=None):
    return LogicalSession(name, repo, url, f"{WS}/{repo}", f"id-{name}", 0.0, t)


@pytest.fixture(autouse=True)
def nothing_left_behind(monkeypatch):
    """LAYOUT is a real path: keep resolve_target off this host's disk and git."""
    from cloud_coder_vm import launch

    monkeypatch.setattr(launch, "left_behind", lambda layout, repo, index: False)


def resolve(sessions, **kw):
    args = {"repo_url": None, "repo": None, "session_name": None, "new": False}
    args |= {"has_prompt": False, "now": 100.0}
    args.update(kw)
    return resolve_target(sessions, LAYOUT, **args)


def test_first_connect_creates_session_in_main_checkout():
    t = resolve({}, repo_url="git@github.com:o/app.git")
    assert t.created
    assert t.session.name == "cc-app-1"
    assert t.session.workdir == str(WS / "app")
    assert t.session.repo_url == "git@github.com:o/app.git"


def test_reconnect_reuses_latest_session_of_repo():
    s = {"cc-app-1": entry("cc-app-1", "app", 1), "cc-app-2": entry("cc-app-2", "app", 5)}
    t = resolve(s, repo_url="git@github.com:o/app.git")
    assert not t.created and t.session.name == "cc-app-2"


def test_new_creates_worktree_session_and_never_reuses():
    s = {"cc-app-1": entry("cc-app-1", "app", 1, url="u")}
    t = resolve(s, repo="app", new=True)
    assert t.created and t.session.name == "cc-app-2"
    assert t.session.workdir == "/home/coder/git/wt/app-2"
    assert t.session.repo_url == "u"
    assert t.session.claude_session_id != s["cc-app-1"].claude_session_id


def test_no_repo_means_latest_session():
    s = {"cc-a-1": entry("cc-a-1", "a", 1), "cc-b-1": entry("cc-b-1", "b", 3)}
    assert resolve(s).session.name == "cc-b-1"
    with pytest.raises(LaunchError):
        resolve({})


def test_explicit_session():
    s = {"cc-a-1": entry("cc-a-1", "a", 1)}
    assert resolve(s, session_name="cc-a-1").session.name == "cc-a-1"
    with pytest.raises(LaunchError):
        resolve(s, session_name="cc-x-9")


def test_claude_command_new_vs_resume():
    s = entry("cc-a-1", "a", 1)
    new = claude_command(Path("/home/coder"), s, resume=False)
    assert new == (
        "env -u TMUX CLOUD_CODER_SESSION=cc-a-1 /home/coder/.local/bin/claude "
        "--session-id id-cc-a-1 --remote-control cc-a-1"
    )
    assert "--resume id-cc-a-1" in claude_command(Path("/home/coder"), s, resume=True)


def test_claude_command_reads_first_prompt_from_file_verbatim(tmp_path):
    import subprocess

    s = entry("cc-a-1", "a", 1)
    prompt = "line 1 'single' \"double\" $HOME `id`\n-starts-with-dash\n日本語"
    prompt_file = tmp_path / "p 1.txt"
    prompt_file.write_text(prompt)
    command = claude_command(Path("/home/coder"), s, resume=False, prompt_file=prompt_file)
    assert command.endswith(f"-- \"$(cat '{prompt_file}'; rm -f '{prompt_file}')\"")
    # run the same shell expansion with printf standing in for claude
    probe = command.replace("/home/coder/.local/bin/claude", "printf '%s\\0'")
    out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True)
    argv = out.stdout.split("\0")[:-1]
    assert argv[-2:] == ["--", prompt]
    assert not prompt_file.exists()


def test_regroup_command(monkeypatch):
    import grp

    from cloud_coder_vm.launch import regroup_command

    monkeypatch.setattr(grp, "getgrnam", lambda name: type("G", (), {"gr_gid": 999})())
    assert regroup_command("docker", {999}, {1000}) == [
        "sg",
        "docker",
        "-c",
        'exec "${SHELL:-/bin/bash}" -l',
    ]
    assert regroup_command("docker", {999}, {999}) == []  # server already has it
    assert regroup_command("docker", {999}, None) == []  # new server inherits it
    assert regroup_command("docker", {1000}, {1000}) == []  # this login lacks it too


def test_same_repo_name_from_another_owner_is_refused():
    s = {"cc-app-1": entry("cc-app-1", "app", 1, url="git@github.com:me/app.git")}
    assert not resolve(s, repo_url="https://github.com/me/app").created  # same repo
    with pytest.raises(LaunchError, match="cloned from"):
        resolve(s, repo_url="git@github.com:someone-else/app.git")


def git(*args, cwd):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_clone(path, origin):
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git(
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=a",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=path,
    )
    git("remote", "add", "origin", origin, cwd=path)


def tmp_layout(tmp_path):
    return Layout(tmp_path / "git", tmp_path / "git" / "wt", tmp_path / "workspace")


def test_trust_roots_have_no_duplicates(tmp_path):
    layout = tmp_layout(tmp_path)
    assert layout.trust_roots() == [tmp_path / "git", tmp_path / "git/wt", tmp_path / "workspace"]
    same = Layout(tmp_path / "workspace", tmp_path / "wt", tmp_path / "workspace")
    assert same.trust_roots() == [tmp_path / "workspace", tmp_path / "wt"]


def test_legacy_clone_is_reused_and_new_worktrees_come_from_it(tmp_path):
    from cloud_coder_vm.launch import ensure_repo, main_checkout

    layout = tmp_layout(tmp_path)
    legacy = tmp_path / "workspace" / "app"
    make_clone(legacy, "git@github.com:me/app.git")
    assert main_checkout(layout, "app") == legacy
    s1 = LogicalSession("cc-app-1", "app", "u", str(legacy), "id", 0.0, 0.0)
    assert ensure_repo(s1, layout) == "existing"
    wt = tmp_path / "git" / "wt" / "app-2"
    s2 = LogicalSession("cc-app-2", "app", "git@github.com:me/app.git", str(wt), "id2", 0.0, 0.0)
    assert ensure_repo(s2, layout, created=True) == "worktree-added"
    assert not (tmp_path / "git" / "app").exists()  # no second clone


def test_new_session_does_not_adopt_a_directory_it_did_not_create(tmp_path):
    from cloud_coder_vm.launch import ensure_repo

    layout = tmp_layout(tmp_path)
    taken = tmp_path / "git" / "wt" / "app-2"
    taken.mkdir(parents=True)
    s2 = LogicalSession("cc-app-2", "app", None, str(taken), "id2", 0.0, 0.0)
    with pytest.raises(LaunchError, match="not created by cloud-coder"):
        ensure_repo(s2, layout, created=True)


def test_existing_clone_is_adopted_only_for_the_same_repository(tmp_path):
    from cloud_coder_vm.launch import ensure_repo

    layout = tmp_layout(tmp_path)
    clone = tmp_path / "git" / "app"
    make_clone(clone, "git@github.com:me/app.git")

    def session(url):
        return LogicalSession("cc-app-1", "app", url, str(clone), "id", 0.0, 0.0)

    assert ensure_repo(session("https://github.com/me/app"), layout, created=True) == "existing"
    assert ensure_repo(session(None), layout, created=True) == "existing"  # named by the user
    with pytest.raises(LaunchError, match="not a clone of"):
        ensure_repo(session("git@github.com:other/app.git"), layout, created=True)


def test_legacy_clone_stays_the_main_checkout_when_a_current_one_appears(tmp_path):
    from cloud_coder_vm.launch import main_checkout

    layout = tmp_layout(tmp_path)
    make_clone(tmp_path / "workspace" / "app", "git@github.com:me/app.git")
    make_clone(tmp_path / "git" / "app", "git@github.com:other/app.git")  # cloned by hand
    assert main_checkout(layout, "app") == tmp_path / "workspace" / "app"
    assert main_checkout(layout, "lib") == tmp_path / "git" / "lib"


def test_new_worktree_is_refused_from_a_clone_of_another_repository(tmp_path):
    from cloud_coder_vm.launch import ensure_repo

    layout = tmp_layout(tmp_path)
    make_clone(tmp_path / "git" / "app", "git@github.com:other/app.git")
    wt = tmp_path / "git" / "wt" / "app-2"
    s2 = LogicalSession("cc-app-2", "app", "git@github.com:me/app.git", str(wt), "id", 0.0, 0.0)
    with pytest.raises(LaunchError, match="not a clone of"):
        ensure_repo(s2, layout, created=True)
    assert not wt.exists()


def test_trust_only_for_clones_verified_against_the_session_url(tmp_path):
    import json

    from cloud_coder_vm.launch import trust_session

    home = tmp_path
    layout = tmp_layout(tmp_path)
    clone = tmp_path / "git" / "app"
    make_clone(clone, "git@github.com:me/app.git")
    claude_json = home / ".claude.json"

    def trusted():
        if not claude_json.exists():
            return set()
        return set(json.loads(claude_json.read_text()).get("projects", {}))

    by_name = LogicalSession("cc-app-1", "app", None, str(clone), "id", 0.0, 0.0)
    assert trust_session(by_name, layout, home).startswith("skipped")  # every connect, not once
    assert trust_session(by_name, layout, home).startswith("skipped")
    other = LogicalSession("cc-app-1", "app", "git@github.com:x/app.git", str(clone), "i", 0, 0)
    assert trust_session(other, layout, home).startswith("skipped")
    assert trusted() == set()
    same = LogicalSession("cc-app-1", "app", "https://github.com/me/app", str(clone), "i", 0, 0)
    assert trust_session(same, layout, home) is True
    assert trusted() == {str(clone.resolve())}


def test_existing_session_does_not_add_a_worktree_from_a_foreign_clone(tmp_path):
    from cloud_coder_vm.launch import ensure_repo

    layout = tmp_layout(tmp_path)
    make_clone(tmp_path / "workspace" / "app", "git@github.com:other/app.git")  # appeared later
    wt = tmp_path / "git" / "wt" / "app-2"
    s2 = LogicalSession("cc-app-2", "app", "git@github.com:me/app.git", str(wt), "id", 0.0, 0.0)
    with pytest.raises(LaunchError, match="not a clone of"):
        ensure_repo(s2, layout, created=False)


# A prompt is a new task unless a session is named (issue #18).


def test_prompt_without_session_starts_a_new_session_never_the_latest():
    s = {"cc-app-1": entry("cc-app-1", "app", 5, url="u")}
    t = resolve(s, repo="app", has_prompt=True)
    assert t.created and t.session.name == "cc-app-2"
    assert t.session.workdir == "/home/coder/git/wt/app-2"
    assert t.session.claude_session_id != s["cc-app-1"].claude_session_id


def test_prompt_for_a_repo_without_sessions_uses_the_main_checkout():
    t = resolve({}, repo_url="git@github.com:o/app.git", has_prompt=True)
    assert t.created and t.session.name == "cc-app-1"
    assert t.session.workdir == str(WS / "app")


def test_prompt_with_session_continues_that_session():
    s = {"cc-app-1": entry("cc-app-1", "app", 1), "cc-app-2": entry("cc-app-2", "app", 5)}
    t = resolve(s, session_name="cc-app-1", has_prompt=True)
    assert not t.created and t.session is s["cc-app-1"]


def test_prompt_without_repo_or_session_is_refused_not_sent_to_the_latest():
    s = {"cc-app-1": entry("cc-app-1", "app", 5)}
    with pytest.raises(LaunchError, match="needs a repository"):
        resolve(s, has_prompt=True)


def test_launch_hands_a_prompt_to_a_new_session(tmp_path, monkeypatch):
    """The whole VM-side path: a prompt without a session reaches a new session."""
    import contextlib

    from cloud_coder_vm import launch as launch_mod
    from cloud_coder_vm import session_registry
    from cloud_coder_vm.system_files import VmConfig

    monkeypatch.setattr(launch_mod.busy_markers, "busy_marker", contextlib.nullcontext)
    monkeypatch.setattr(launch_mod, "state_lock", contextlib.nullcontext)
    monkeypatch.setattr(launch_mod.idle_check, "cancel_grace", lambda: None)
    monkeypatch.setattr(launch_mod, "ensure_repo", lambda *a: "existing")
    monkeypatch.setattr(launch_mod, "ensure_tmux_session", lambda s: "existing")
    handed = {}

    def fake_ensure_claude(session, home, prompt, reuse_pane):
        handed[session.name] = prompt
        return {"claude": "started", "conversation": "new"}

    monkeypatch.setattr(launch_mod, "ensure_claude", fake_ensure_claude)
    registry = launch_mod.paths.registry_path(tmp_path)
    registry.parent.mkdir(parents=True)
    session_registry.save(registry, {"cc-app-1": entry("cc-app-1", "app", 5, url="u")})
    config = VmConfig(
        "coder", 0, "git", "git/wt", False, 0, [], False, False, 0, False, None, None, ""
    )

    def run(**kw):
        return launch_mod.launch(config, tmp_path, repo="app", **kw)

    assert run(prompt="task")["session"] == "cc-app-2"
    assert run(prompt="more", session_name="cc-app-1")["session"] == "cc-app-1"
    assert run()["session"] == "cc-app-1"  # no prompt: the latest session (just connected)
    assert handed == {"cc-app-2": "task", "cc-app-1": None}


def fake_tmux(monkeypatch, *, claude_running: bool, transcript: bool):
    from cloud_coder_vm import launch as launch_mod
    from cloud_coder_vm.process_table import Process

    claude = Process(20, 10, "claude")
    shell = Process(10, 1, "bash")
    sent = []

    def check(args, cwd=None):
        if "list-panes" in args:
            return "cc-app-1\t%1\t10\tbash\n"
        sent.append(args)
        return "%2\n"

    monkeypatch.setattr(launch_mod, "_check", check)
    monkeypatch.setattr(launch_mod.process_table, "snapshot", lambda: {})
    monkeypatch.setattr(launch_mod.process_table, "children_map", lambda procs: {})
    monkeypatch.setattr(
        launch_mod.process_table,
        "pane_subtree",
        lambda pid, procs, children: ([claude], []) if claude_running else ([], [shell]),
    )
    monkeypatch.setattr(launch_mod, "_cmdline", lambda pid: ["claude", "--remote-control"])
    monkeypatch.setattr(launch_mod, "send_prompt_to_running", lambda *a: sent.append(a) or "queued")
    monkeypatch.setattr(launch_mod, "transcript_exists", lambda home, sid: transcript)
    monkeypatch.setattr(launch_mod, "write_prompt_file", lambda s, p: Path("/run/p.txt"))
    return sent


@pytest.mark.parametrize(
    ("claude_running", "transcript", "claude", "conversation"),
    [
        (False, False, "started", "new"),
        (False, True, "resumed", "continued"),
        (True, True, "running", "continued"),
    ],
)
def test_result_says_whether_the_conversation_is_new_or_continued(
    monkeypatch, claude_running, transcript, claude, conversation
):
    from cloud_coder_vm.launch import ensure_claude

    fake_tmux(monkeypatch, claude_running=claude_running, transcript=transcript)
    result = ensure_claude(entry("cc-app-1", "app", 1), Path("/home/coder"), "go", True)
    assert (result["claude"], result["conversation"]) == (claude, conversation)
    assert result["prompt"] == ("queued" if claude_running else "passed-at-start")


def test_claude_runs_without_tmux_but_keeps_its_pane(tmp_path):
    """Claude Code's tools must not reach cloud-coder's tmux server through $TMUX; the
    hook still needs $TMUX_PANE."""
    import subprocess

    s = entry("cc-a-1", "a", 1)
    probe = claude_command(Path("/home/coder"), s, resume=False).replace(
        "/home/coder/.local/bin/claude", "printenv"
    )
    probe = probe.split(" --session-id")[0]
    env = {"PATH": "/usr/bin:/bin", "TMUX": "/tmp/tmux-1000/cloud-coder,1,0", "TMUX_PANE": "%3"}
    out = subprocess.run(["bash", "-c", probe], env=env, capture_output=True, text=True).stdout
    assert "TMUX=" not in out.replace("TMUX_PANE=", "")
    assert "TMUX_PANE=%3" in out and "CLOUD_CODER_SESSION=cc-a-1" in out


def test_a_session_left_on_the_default_tmux_server_is_not_started_twice(monkeypatch):
    import subprocess

    from cloud_coder_vm import launch

    calls = []

    def fake_run(args, cwd=None):
        calls.append(args)
        found = args[:3] == ["tmux", "-L", "default"]
        return subprocess.CompletedProcess(args, 0 if found else 1, "", "")

    monkeypatch.setattr(launch, "_run", fake_run)
    with pytest.raises(LaunchError, match="default tmux server"):
        launch.ensure_tmux_session(entry("cc-a-1", "a", 1))
    assert calls[0][:3] == ["tmux", "-L", "cloud-coder"]


# Prompts for a Claude Code that already runs (issue #35): typed while READY / IDLE, queued
# by Claude Code itself while BUSY, never typed into a dialog, and confirmed by the hook.


class FakeClaude:
    """tmux and a Claude Code behind it: Enter submits what was pasted, which fires the
    real hook (UserPromptSubmit) unless a dialog is shown or ``confirms`` is off."""

    PANE = "%7"
    PID = 700

    def __init__(self, tmp_path, monkeypatch):
        from cloud_coder_vm import launch, paths
        from cloud_coder_vm.process_table import Process

        self.tmp_path = tmp_path
        self.states = tmp_path / "sessions"
        self.states.mkdir()
        monkeypatch.setattr(paths, "SESSION_STATE_DIR", self.states)
        monkeypatch.setattr(paths, "PROMPT_DIR", tmp_path / "prompts")
        monkeypatch.setattr(launch, "PROMPT_CONFIRM_SECONDS", 1.0)
        monkeypatch.setattr(launch, "_check", self.check)
        monkeypatch.setattr(launch.subprocess, "run", self.run)
        self.proc = Process(self.PID, 1, "claude", "", 1)
        self.ops: list[str] = []
        self.buffers: dict[str, str] = {}
        self.input = ""
        self.submitted: list[str] = []
        self.confirms = True
        self.on_paste = None

    def event(self, payload):
        from cloud_coder_vm.hook import apply_event

        env = {"TMUX_PANE": self.PANE, "CLOUD_CODER_SESSION": "cc-app-1"}
        payload = {"session_id": "S", **payload}
        return apply_event(payload, env, self.states, self.tmp_path / "reg.json", self.proc)

    def state(self):
        from cloud_coder_vm import session_state

        return session_state.load(self.states, "pane-7")

    def run(self, args, input=None, text=None, check=None):
        import subprocess

        assert "load-buffer" in args and args[-1] == "-"
        self.buffers[args[args.index("-b") + 1]] = input
        return subprocess.CompletedProcess(args, 0, "", "")

    def check(self, args, cwd=None):
        if "paste-buffer" in args:
            assert "-p" in args  # bracketed paste: newlines stay in the prompt
            self.ops.append("paste")
            self.input += self.buffers.pop(args[args.index("-b") + 1])
            if self.on_paste:
                self.on_paste()
        elif args[-1] == "Enter":
            self.ops.append("enter")
            if self.confirms and not self.state().open_dialogs:
                self.submitted.append(self.input)
                self.event({"hook_event_name": "UserPromptSubmit", "prompt": self.input})
            self.input = ""
        return ""

    def send(self, prompt):
        from cloud_coder_vm.launch import send_prompt_to_running

        return send_prompt_to_running(self.PANE, self.PID, prompt)


@pytest.fixture
def claude(tmp_path, monkeypatch):
    return FakeClaude(tmp_path, monkeypatch)


STOP = {"hook_event_name": "Stop", "background_tasks": [], "session_crons": []}
WRITE = {"tool_name": "Write", "tool_input": {"file_path": "/w/a.txt", "content": "x"}}


def test_prompt_to_a_waiting_claude_is_sent(claude):
    claude.event({"hook_event_name": "SessionStart", "source": "startup"})  # IDLE
    assert claude.send("first") == "sent"
    claude.event(STOP)  # READY
    assert claude.send("second") == "sent"
    assert claude.submitted == ["first", "second"]


def test_prompt_to_a_busy_claude_is_queued_in_order(claude):
    claude.event({"hook_event_name": "UserPromptSubmit"})  # BUSY
    prompt = "日本語の追加指示 🚀\nline 2 with $HOME `id` \"dq\" 'sq' <tag> & | ;\n\tindented"
    assert claude.send(prompt) == "queued"
    assert claude.send("then this") == "queued"
    assert claude.submitted == [prompt, "then this"]
    assert claude.state().prompts_submitted == 3


def test_concurrent_prompts_are_not_merged(claude):
    from concurrent.futures import ThreadPoolExecutor

    claude.event({"hook_event_name": "UserPromptSubmit"})
    prompts = [f"prompt {i}" for i in range(4)]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(claude.send, prompts))
    assert results == ["queued"] * 4
    assert sorted(claude.submitted) == prompts  # each one whole, none twice
    assert claude.ops == ["paste", "enter"] * 4


def test_nothing_is_typed_before_claude_reports_its_state(claude):
    with pytest.raises(LaunchError, match="not reported its state yet; prompt not sent"):
        claude.send("go")
    assert claude.ops == []


@pytest.mark.parametrize(
    "shown",
    [
        {"hook_event_name": "PermissionRequest", **WRITE},
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": []},
        },
        {"hook_event_name": "Elicitation", "mcp_server_name": "srv", "message": "?"},
    ],
)
def test_nothing_is_typed_into_a_dialog(claude, shown):
    """Enter would answer the dialog (seen with Claude Code 2.1.294: it approved a Write)."""
    claude.event({"hook_event_name": "UserPromptSubmit"})
    claude.event(shown)
    with pytest.raises(LaunchError, match="permission prompt or a question; prompt not sent"):
        claude.send("go")
    assert claude.ops == []


def test_prompt_is_queued_once_the_dialog_is_answered(claude):
    claude.event({"hook_event_name": "UserPromptSubmit"})
    claude.event({"hook_event_name": "PermissionRequest", **WRITE})
    other = {"tool_name": "Write", "tool_input": {"file_path": "/w/b.txt", "content": "y"}}
    claude.event({"hook_event_name": "PostToolUse", **other})  # a parallel call, no dialog
    with pytest.raises(LaunchError, match="prompt not sent"):
        claude.send("go")
    claude.event({"hook_event_name": "PostToolUse", **WRITE, "tool_response": {}})
    assert claude.send("go") == "queued"


def test_a_dialog_denied_without_an_event_blocks_until_the_turn_ends(claude):
    claude.event({"hook_event_name": "UserPromptSubmit"})
    claude.event({"hook_event_name": "PermissionRequest", **WRITE})  # then Esc: no event
    with pytest.raises(LaunchError, match="prompt not sent"):
        claude.send("go")
    claude.event(STOP)
    assert claude.send("go") == "sent"


def test_no_enter_when_a_dialog_opens_while_typing(claude):
    claude.event({"hook_event_name": "UserPromptSubmit"})
    claude.on_paste = lambda: claude.event({"hook_event_name": "PermissionRequest", **WRITE})
    with pytest.raises(LaunchError, match="prompt not submitted"):
        claude.send("go")
    assert claude.ops == ["paste"]


def test_unconfirmed_prompt_is_not_reported_as_sent(claude):
    claude.event({"hook_event_name": "UserPromptSubmit"})
    claude.confirms = False
    with pytest.raises(LaunchError, match="prompt not confirmed"):
        claude.send("go")
