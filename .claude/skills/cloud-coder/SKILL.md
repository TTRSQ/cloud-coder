---
name: cloud-coder
description: cloud-coder の GCE VM 上の Claude Code に作業を任せる。VM の状態確認・起動、リポジトリのセッションへのプロンプト送信 (detach)、セッション画面の読み取り、VM の停止を、このリポジトリの cloud-coder CLI (uv run) と MCP Inspector CLI で行う。
argument-hint: "[status | up | <repo> <指示> | read <session> | stop]"
disable-model-invocation: true
model: sonnet
allowed-tools:
  - Bash(uv run cloud-coder status)
  - Bash(uv run cloud-coder status *)
  - Bash(uv run cloud-coder up)
  - Bash(uv run cloud-coder connect *)
  - Bash(npx -y @modelcontextprotocol/inspector --cli uv run cloud-coder mcp *)
---

# cloud-coder に作業を任せる

ユーザーの依頼: $ARGUMENTS

cloud-coder は、GCE VM 上の tmux で Claude Code を動かし、全セッションが idle になると VM を自動停止する CLI です。
このスキルは、このリポジトリの cloud-coder を `uv run cloud-coder` で実行し、VM 上の Claude Code (セッション) に作業を渡して結果を読みます。
コマンドはこのリポジトリの中 (どのディレクトリでもよい) で実行します。`uv run` が上位の `pyproject.toml` を探して、このチェックアウトの cloud-coder を使います。
ユーザーへの返答は日本語で、短く書きます。

## 守ること

- **対象の VM を変えない。** `--project` / `--zone` / `--instance` / `--config` / `--machine-type` などのオプションは付けない。対象は `~/.config/cloud-coder/config.yaml` で決まっている。
- **感嘆符 (!) で始まるプロンプトは送らない。** 先頭の空白を除いた最初の文字が感嘆符の指示は、VM 上の Claude Code がシェルコマンドとして実行してしまう。送らずに、ユーザーに理由を伝えて言い直してもらう。
  <!-- このファイルでは感嘆符の直後にバッククォートを置かない。スキルの shell 埋め込み構文として実行されるため。 -->

- **VM はユーザーが停止を頼んだときだけ止める。** 作業を渡した後や読み取った後に `stop` しない。idle になれば VM は自動で止まる (既定で約 10 分後)。
- 下の「操作」にあるコマンドだけを使う。VM に `gcloud compute ssh` で入ったり、`gcloud` で VM を操作したりしない。
- 失敗したら、エラー出力をそのままユーザーに見せて止まる。同じコマンドを何度も再試行しない。
- `uv run cloud-coder up` と `uv run cloud-coder connect` は、VM が止まっていると起動を待つので数分かかる。Bash の timeout を 600000 (10 分) にして実行する。

## 依頼の読み方

`$ARGUMENTS` を次のどれかに当てはめる。

| 依頼 | 操作 |
| --- | --- |
| 空、`status`、「状態」など | [状態を見る](#状態を見る) |
| `up`、「起動」 | [VM を起動する](#vm-を起動する) |
| リポジトリ (URL か名前) と指示 | [セッションに指示を渡す](#セッションに指示を渡す) |
| セッション名と指示 (「cc-foo-1 に続きを…」) | [セッションに指示を渡す](#セッションに指示を渡す) (`--session`) |
| `read`、「結果」「進み具合」 (+ セッション名) | [セッションの画面を読む](#セッションの画面を読む) |
| `stop`、「止めて」 | [VM を止める](#vm-を止める) |

どれか判断できないとき、またはリポジトリが分からないときは、実行せずにユーザーに聞く。

## 操作

### 状態を見る

```bash
uv run cloud-coder status
```

出力をそのまま要約する。1 行目が VM の状態 (`running` / `stopped` など)。`running` なら、セッションごとに Claude Code の状態 (`BUSY` 作業中 / `READY` `IDLE` 入力待ち) と、`auto-stop` (止まるまでの見込み、止まらない理由) が出る。
このコマンドは VM を起動しない。

### VM を起動する

```bash
uv run cloud-coder up
```

起動して agent の準備まで待つ。セッションは作らない。

### セッションに指示を渡す

指示は heredoc で標準入力から渡す (引用符・`$`・複数行をそのまま届けるため)。区切りの `'CLOUD_CODER_PROMPT'` は必ず引用符付きで書く。

新しく、またはリポジトリの直近のセッションで:

```bash
uv run cloud-coder connect <REPO> --detach --prompt-file - <<'CLOUD_CODER_PROMPT'
<ユーザーの指示>
CLOUD_CODER_PROMPT
```

名前を指定したセッションに追加の指示を送る:

```bash
uv run cloud-coder connect --session <SESSION> --detach --prompt-file - <<'CLOUD_CODER_PROMPT'
<ユーザーの指示>
CLOUD_CODER_PROMPT
```

- `<REPO>` は git URL (`https://github.com/OWNER/REPO.git`) か、VM に clone 済みのリポジトリ名 (`REPO`)。初めてのリポジトリは URL で渡す。
- ユーザーが「別セッションで」「並行して」と言ったときだけ `--new` を付ける (同じリポジトリの 2 つ目のセッションを git worktree に作る)。
- 指示はユーザーの言葉をそのまま渡す。勝手に要約したり書き足したりしない。
- 成功すると、標準出力の最後に JSON が 1 行出る。`session` (セッション名、例 `cc-REPO-1`) をユーザーに伝え、結果は後で `/cloud-coder read <session>` で読めると案内する。
- `Claude Code in this session is BUSY; prompt not sent` で失敗したら、前の作業がまだ終わっていない。送り直さずにユーザーに伝える。
- 標準エラーに `workspace trust skipped` が出たら、Claude Code が trust 画面で止まっている。ユーザー自身のターミナルで、このリポジトリから `uv run cloud-coder connect --session <SESSION>` で attach して答えるよう伝える (attach は対話操作なので、このスキルからは実行しない)。
- 結果を待たない。渡したら報告して終わる。

### セッションの画面を読む

```bash
npx -y @modelcontextprotocol/inspector --cli uv run cloud-coder mcp --method tools/call --tool-name read_session --tool-arg session=<SESSION> --tool-arg lines=200
```

- セッション名が分からなければ、先に `uv run cloud-coder status` で確認する。セッションが 1 つだけならそれを使い、複数あれば一覧を見せて聞く。
- 出力は JSON。`content[0].text` が JSON 文字列で、`output` (画面の文字列) と `claude_state` (Claude Code の状態) が入っている。
- `BUSY` ならまだ作業中なので、途中経過として最後の部分を要約する。`READY` / `IDLE` なら Claude Code の最後の応答を要約し、必要なら該当部分を引用する。
- 画面に権限の確認やログイン画面が出ているときは、それを伝え、ユーザー自身のターミナルで `uv run cloud-coder connect --session <SESSION>` で attach して答えるよう案内する。
- `the VM is stopped; nothing to read` は VM が止まっている (作業を終えて自動停止した後など)。VM を起動して読み直すかをユーザーに聞く。起動するなら `uv run cloud-coder up` の後にもう一度読む。
- このコマンドは VM を起動しない。

### VM を止める

1. まず `uv run cloud-coder status` を実行する。
2. `BUSY` のセッションがあれば、止めると作業が中断されることを伝え、止めずに確認を求める。ユーザーが「作業中でも止めて」などと明示していれば次へ進む。
3. 止める。

```bash
uv run cloud-coder stop
```

disk は残り、次の `connect` で同じセッションを再開できる。
