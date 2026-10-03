import base64
import io
import shlex

from cloud_coder import cli
from cloud_coder.connect import launch_command


def test_launch_command_carries_prompt_as_base64():
    prompt = "fix it\n'quotes' $(rm -rf /) 日本語"
    argv = shlex.split(launch_command("git@github.com:o/r.git", None, False, False, prompt))
    assert argv[argv.index("--repo-url") + 1] == "git@github.com:o/r.git"
    assert base64.b64decode(argv[argv.index("--prompt-b64") + 1]).decode() == prompt


def test_launch_command_without_prompt():
    assert "--prompt-b64" not in launch_command("r", None, True, False)


def test_cli_prompt_sources(tmp_path, monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(["connect", "r", "-p", "hello", "--detach"])
    assert cli.read_prompt(args) == "hello" and args.no_attach
    f = tmp_path / "p.txt"
    f.write_text("from file\nline 2")
    assert cli.read_prompt(parser.parse_args(["connect", "--prompt-file", str(f)])) == (
        "from file\nline 2"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert cli.read_prompt(parser.parse_args(["connect", "--prompt-file", "-"])) == "from stdin"
    assert cli.read_prompt(parser.parse_args(["connect"])) is None
