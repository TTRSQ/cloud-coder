from cloud_coder_vm.tmux_panes import Pane, no_server, parse_list_panes


def test_parse_list_panes():
    out = "cc-repo-1\t%0\t1234\tbash\ncc-repo-1\t%3\t1300\t2.1.300\nbroken line\n"
    assert parse_list_panes(out) == [
        Pane("cc-repo-1", "%0", 1234, "bash"),
        Pane("cc-repo-1", "%3", 1300, "2.1.300"),
    ]


def test_no_server_messages():
    assert no_server("no server running on /tmp/tmux-1000/default")
    assert no_server("error connecting to /tmp/tmux-1000/default (No such file or directory)")
    assert not no_server("error connecting to /tmp/tmux-1000/default (Permission denied)")
