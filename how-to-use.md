# cloud-coder の使い方

やりたいこと別の手順書です。仕組みや設定項目の一覧は [README](README.md) を参照してください。

- [初回セットアップ](#初回セットアップ)
- [毎日の使い方](#毎日の使い方)
- [タスクを投げて放置する](#タスクを投げて放置する)
- [複数のセッションを並行して使う](#複数のセッションを並行して使う)
- [スマホなどから Remote Control で操作する](#スマホなどから-remote-control-で操作する)
- [MCP server として使う](#mcp-server-として使う)
- [HTTP API として使う](#http-api-として使う)
- [Claude Code のスキルで操作する](#claude-code-のスキルで操作する)
- [自動停止を使いこなす](#自動停止を使いこなす)
- [停止した VM で作業を再開する](#停止した-vm-で作業を再開する)
- [マシンスペックを変える](#マシンスペックを変える)
- [トラブルシューティングと注意点](#トラブルシューティングと注意点)
- [片付ける](#片付ける)

以下、VM 名・zone は既定値 (`cloud-coder`、`asia-northeast1-b`) で書きます。変えている場合は読み替えてください。

## 初回セットアップ

### 1. CLI を入れる

[必要なもの](README.md#必要なもの) (uv、gcloud、firewall、Claude Code のプラン) を確認してから入れます。

```bash
uv tool install git+https://github.com/TTRSQ/cloud-coder.git
cloud-coder --help
```

### 2. gcloud を認証する

```bash
gcloud auth login
```

cloud-coder は `gcloud config` の既定 project を使いません。対象 project は次の config.yaml の `gcp.project` (または `--project`) で指定します。

### 3. config.yaml を書く

`~/.config/cloud-coder/config.yaml` に置きます。`gcp.project` は必須で、無ければ各コマンドはエラーで止まります。他のキーは省略すると既定値になります。よく変えるのは次のキーです (全項目と既定値は [README の設定](README.md#設定))。

```yaml
gcp:
  project: my-project          # 必須
  zone: asia-northeast1-b
  machine_type: t2d-standard-8 # VM 作成時に使う
  disk_size_gb: 100            # VM 作成時に使う
vm:
  idle_grace_minutes: 10       # idle になってから停止までの猶予
  tools: [gh, node, rust, docker, uv]   # VM に入れる開発ツール
claude:
  dotfiles_repo: https://github.com/OWNER/dotClaude.git   # Claude Code の設定リポジトリ (任意)
```

- 知らないセクションやキーを書くとエラーになります (`unknown config section ...` / `unknown config key ...`)。
- `gcp.project` / `gcp.zone` / `gcp.instance` は操作対象の VM を決める値で、毎回使われます。VM を作った後に変えると別の VM として扱われ、次の `up` / `connect` で新しい VM が作られます (元の VM と disk は残り、課金も続きます)。`--project` / `--zone` / `--instance` を付けたときも同じです。各コマンドは最初に `cloud-coder: target project=... zone=... instance=...` を標準エラー出力に表示するので、意図した VM か確認できます。不要になった VM は[片付け](#片付ける)てください。
- `gcp.machine_type` / `disk_size_gb` / `disk_type` / `image_family` / `image_project` は VM を作るときにだけ使われます。作成後の変更は[マシンスペックを変える](#マシンスペックを変える)を参照してください。
- `vm.*` / `git.*` / `claude.*` は、次の `up` / `connect` で VM に送られて反映されます。

### 4. VM を作り GitHub を認証する

VM 上の `gh` の認証は対話操作なので cloud-coder は行いません。private repository を clone する前に済ませておくと、手戻りがありません。

```bash
cloud-coder up    # VM を作成し、agent と開発ツール、Claude Code を入れる
```

`claude.dotfiles_repo` が private repository の場合、この時点では clone に失敗して警告が出ますが、先へ進みます (次の `up` / `connect` で再試行されます)。

続いて VM に SSH で入り、gh を認証します。cloud-coder と同じく IAP 経由 (`--tunnel-through-iap`) で入ります (`ssh.iap: false` にしている場合は外します)。

```bash
gcloud compute ssh coder@cloud-coder --project <project> --zone asia-northeast1-b --tunnel-through-iap
gh auth login        # GitHub.com → HTTPS → ブラウザ (表示されるコードをローカルのブラウザで入力) か token
gh auth setup-git    # git の HTTPS 認証に gh を使う
```

- プロトコルは HTTPS を選んでください。既定 (`git.github_https: true`) では VM の git が `git@github.com:...` 形式の URL も HTTPS に読み替えるので、SSH URL で `connect` しても clone / push は gh の認証で行われます。
- token で認証する場合、fine-grained personal access token なら次を満たすようにしてください。足りないと clone や push が失敗します。
  - 作業するリポジトリが token の対象 (Repository access) に入っていること。push するなら Contents の Read and write、`gh pr create` などを使うなら Pull requests の権限も付けます。
  - `claude.dotfiles_repo` (dotClaude など) が private なら、そのリポジトリも対象に入れ、少なくとも Contents の Read を付けること。
- 認証情報は VM の `~/.config/gh` (Persistent Disk) に残るので、作業は初回だけです。

dotfiles に含めていない `~/.claude/.env` (API キーなど) は cloud-coder がコピーしないので、必要なら手で置きます。SSH したまま VM 上でエディタで作るか、ローカルのファイルを送ります。

```bash
# VM 上で: 直接作る
mkdir -p ~/.claude && vi ~/.claude/.env && chmod 600 ~/.claude/.env

# ローカルから: 既存のファイルを送る (VM に ~/.claude がある状態で)
gcloud compute scp ~/.claude/.env coder@cloud-coder:~/.claude/.env --project <project> --zone asia-northeast1-b --tunnel-through-iap
```

SSH でログインしている間は VM は自動停止しません ([自動停止](#自動停止を使いこなす))。終わったら `exit` してください。

### 5. 最初の connect と Claude Code へのログイン

```bash
cloud-coder connect https://github.com/OWNER/REPO.git
```

clone → tmux → Claude Code の起動まで行い、tmux に attach します。dotfiles の clone に失敗していた場合はここで再試行されます。

attach したら、tmux の中の Claude Code で次を行います。いずれも VM の `~/.claude` / `~/.claude.json` に保存され、以後は不要です。

1. テーマ選択
2. `/login`
3. Remote Control の初回確認が出たら答える

ログインが済むまで Claude Code は状態を報告できず、VM は自動停止しません。ログイン画面のまま放置しないでください。

## 毎日の使い方

```bash
cloud-coder connect REPO        # REPO の直近のセッションへ (VM が止まっていれば起動する)
cloud-coder connect             # 直近に使ったセッションへ
```

- `REPO` は clone 済みのリポジトリ名 (`~/git/REPO` のディレクトリ名) です。URL を渡しても、そのリポジトリのセッションが既にあれば同じセッションに戻ります (同じ名前の別リポジトリの URL を渡すとエラーになります)。
- 抜けるときは tmux を detach (`Ctrl-b d`) します。Claude Code は tmux の中で動き続けます。SSH が切れた場合も同じで、もう一度 `connect` すれば戻れます。
- `connect` は足りないものだけを用意します。既存のリポジトリを pull / reset することはありません。

### status を見る

```bash
cloud-coder status
cloud-coder status --json   # 機械可読な全情報
```

```text
VM cloud-coder (asia-northeast1-b): running, t2d-standard-8
  auto-stop: blocked
    - claude cc-myapp-1 (%0) is BUSY
  idle-check timer: active, grace 600s
  sessions:
    cc-myapp-1: tmux running, claude BUSY, /home/coder/git/myapp (claude session 0f3c...)
```

| 行 | 意味 |
| --- | --- |
| `VM ...: running` | VM の状態 (`running` / `stopped` / `absent` など)。`running` 以外ならこの行だけ表示されます |
| `agent: unavailable: ...` | VM は動いているが agent に SSH で問い合わせられなかった。以降の行は表示されません |
| `auto-stop: blocked` | 自動停止を妨げているものがある。下の `-` 行が理由です ([理由の読み方](#止まらないときに確認する)) |
| `auto-stop: idle, shutdown in 420s` | idle で、あと 420 秒で停止する |
| `auto-stop: idle, grace period starts at the next check` | idle で、次の判定 (1 分以内) から grace period が始まる |
| `sessions:` | セッションごとの tmux の有無と Claude Code の状態 (`BUSY` / `READY` / `IDLE`、不明なら `-`) |

### 止める・起動だけする

```bash
cloud-coder stop   # すぐに VM を停止する (disk は残る)
cloud-coder up     # VM の作成・起動と agent の更新だけ行う (attach しない)
```

- `stop` は作業中かどうかを確認せずに止めます。Claude Code が作業中ならその作業は中断されます。普段は自動停止に任せれば十分です。
- `up` は tmux も Claude Code も起動しません。VM を起動して `up` だけで放置すると、何も動いていないので判定が始まる起動 2 分後から grace period を経て自動停止します。

## タスクを投げて放置する

```bash
cloud-coder connect REPO -p "テストが落ちている原因を調べて直して" --detach
cloud-coder connect REPO --prompt-file task.md --detach
cat task.md | cloud-coder connect REPO --prompt-file - --detach
```

- プロンプトを渡すと、毎回新しいセッション (2 つ目以降は git worktree) と新しい Claude Code の会話で始まります。以前の会話の続きにはなりません。続きとして渡すときは次の節のように `--session` を指定します。
- 出力の JSON の `created` (セッションを新しく作ったか) と `conversation` (`new` / `continued`) で、新規か継続かを確認できます。
- `-p` / `--prompt` と `--prompt-file` はどちらか一方だけ指定できます。`--prompt-file -` で標準入力から読みます。複数行や引用符、`$` もそのまま届きます。
- `--detach` (`--no-attach` と同じ) は attach せずに戻ります。Claude Code が作業を終えて idle になれば、grace period の後に VM が止まります。
- 結果は後で `connect --session cc-REPO-N` (セッション名は出力の `session` か `cloud-coder status`) で見るか、[Remote Control](#スマホなどから-remote-control-で操作する) で確認します。

### 既存のセッションに続きの指示を渡す

```bash
cloud-coder status                                          # セッション名を確認
cloud-coder connect --session cc-REPO-2 -p "..." --detach   # その会話の続きとして渡す
```

`--session` で指定したセッションの会話に渡します。Claude Code が既に動いていれば、その入力欄に貼り付けて Enter を送ります。

- Claude Code が作業中 (`BUSY`) でも送れます。Claude Code が自分のキューに入れ、作業を止めずに次の区切り (tool の実行が終わったところ) で同じ会話に取り込みます。出力の `prompt` は、入力待ちだったときは `sent`、作業中だったときは `queued` です。終わるのを待ってから送り直す必要はありません。
- 権限の確認や Claude Code からの質問 (ダイアログ) が出ている間と、まだ状態を報告していないときは何も入力せず、`... prompt not sent` のエラーで終了します。attach してダイアログに答えてから送り直してください。ダイアログを `Esc` や `No` で断った後は、画面にダイアログが無くても、Claude Code が作業を終える (ターンが終わる) までは拒否されます。止まったままなら、attach してプロンプトを送り、そのターンが終わってから送り直してください。
- Claude Code が受け取ったことを hook で確認できなかったときは `... prompt not confirmed` のエラーになります。入力欄に残っているか失われているので、画面を見てから送り直してください。
- attach して入力欄に打ちかけている文字列があると、送ったプロンプトとつながって送られます。
- Claude Code が止まっていたセッション (VM 停止後など) では、Claude Code を起動 / resume するときの最初のプロンプトとして渡します。

## 複数のセッションを並行して使う

プロンプトを渡すたびに新しいセッションができるので、タスクを続けて投げればそのまま並行して動きます。プロンプト無しで別のセッションを開くときは `--new` を使います。

```bash
cloud-coder connect REPO -p "..." --detach        # タスクごとに新しいセッション (2 つ目以降は git worktree)
cloud-coder connect REPO --new                    # プロンプト無しで別の Claude Code を git worktree 上に起動
cloud-coder status                                # セッション名を確認
cloud-coder connect --session cc-REPO-2           # 名前を指定して戻る
cloud-coder close cc-REPO-2                       # 終わったセッションを片付ける
```

| セッション | 作業ディレクトリ (VM 上) | branch |
| --- | --- | --- |
| `cc-REPO-1` | `~/git/REPO` (clone) | clone したときのまま |
| `cc-REPO-N` (2 つ目以降) | `~/git/wt/REPO-N` (git worktree) | `cloud-coder/cc-REPO-N` |

- セッション名は tmux の session 名と、Remote Control の名前を兼ねます。セッションは cloud-coder 専用の tmux server で動くので、VM 上で手で tmux を操作するときは `tmux -L cloud-coder ...` とします ([README の tmux server の分離](README.md#tmux-server-の分離))。
- プロンプト無しの `connect REPO` (`--session` 無し) は、そのリポジトリで直近に connect したセッションに戻ります。プロンプト付きなら新しいセッションです。
- `--new` と、`--session` 無しでプロンプトを渡すときは、リポジトリ (名前か URL) が必要です。
- 置き場所は `vm.workspace` / `vm.worktrees` で変えられます ([README のセッション](README.md#セッション))。
- `cloud-coder close cc-REPO-N` でセッションを閉じます。tmux session を Claude Code ごと終了し、worktree と branch `cloud-coder/cc-REPO-N` を削除して、登録から外します。
  - 消えるのは、Git の remote にあるもの (コミット済みで push 済みのファイル) と、ビルドやツールが作り直せるキャッシュだけです。キャッシュとして扱うのは次の ignored のディレクトリとファイルで、名前だけでなく中身も確かめます。

    | パス (worktree 内のどこでも) | キャッシュとみなす条件 |
    | --- | --- |
    | `target/` | cargo が置く `CACHEDIR.TAG` があり、上 2 階層に cargo が書くもの (`debug/`・`release/` などと、その中の `deps/`・`incremental/`・`build/`・`.fingerprint/`、dep-info の `.d`、`deps/` から hard link された成果物。ほかに `doc/`・`package/`・`tmp/`) しか無い。`deps/` や `tmp/` などの中までは見ません |
    | `.venv/` | `pyvenv.cfg` があり、直下に venv / uv が作るもの (`bin/`・`lib/`・`include/` など) しか無い |
    | `node_modules/` | 隣に `package.json` がある |
    | `__pycache__/`、`*.pyc` | `.pyc` しか無い |
    | `.pytest_cache/`、`.ruff_cache/`、`.mypy_cache/` | `CACHEDIR.TAG` がある |

    リポジトリの `.gitignore` に書かれていなくても、ツールが自分で置いた `.gitignore` (`*`) で ignored になっていれば、ディレクトリ全体を 1 つとして判定します (中に Git 管理下のファイルがあるディレクトリは除き、ignored のパスを 1 つずつ判定します)。

  - 次のどれかがあると、何も消さずにエラーにします。エラーには原因のパスが出ます。
    - 未コミットの変更 (untracked のファイルを含む) と、どの remote にも無いコミット
    - キャッシュ以外の ignored のファイルやディレクトリ (`.env`、sqlite などのローカル DB、`out/` の研究データ、`dist/`・`build/`、その他の未知のもの)。`target/` などの名前でも、中に cargo などが書かないもの (手で置いた `target/results.csv` など) があればキャッシュとみなしません。
    - symlink の ignored パス (`target` が別の場所を指しているなど。指す先はもちろん、symlink 自体も消しません)
    - worktree の中の mount point
    - そのセッションの tmux の外で、worktree を使っているプロセス (worktree を作業ディレクトリにしている、そこの実行ファイルを実行している、そこのファイルを開いているか mmap している。例: 別の端末から起動した研究プロセスやビルド、worktree の `.venv/bin/python` で動いているスクリプト)
  - `--dry-run` を付けると、何も変えずに結果だけを JSON で出します。`closable` (閉じられるか)、`blockers` (閉じるのを止めるもの)、`caches` / `caches_bytes` (一緒に消えるキャッシュとその大きさ)、`worktree_size` (閉じたときに worktree 全体で空く容量の見込み。外から hard link されているファイルは数えません)、`tmux` と `ended_processes` (close で終了する tmux session と、その中で Claude Code とシェル以外に動いているプロセス) が分かります。
  - `--discard-ignored` を付けると、キャッシュ以外の ignored のファイルも消して閉じます (symlink はリンク自体だけを消します)。`.env` や `out/` も消えるので、`--dry-run --discard-ignored` で `discarded_ignored` を確かめてから使ってください。未コミットの変更、push していないコミット、ignored のパスの中の Git リポジトリ (`.git`)、mount point、worktree を使っているプロセスがあるときは、このオプションでもエラーにします。close のエラーはこのオプションを案内しません (消してよいかは人が判断するため)。CLI だけのオプションで、MCP / HTTP API からは close 自体を使えません。
  - 閉じるときは、tmux session を終了した後、worktree を使うプロセスが終わるのを最大 15 秒待ち、もう一度全部を確かめてから、キャッシュ (と `--discard-ignored` の ignored ファイル) を消します。そのうえで worktree にコミット済みのファイル以外が何も残っていないことを確かめてから `git worktree remove` (`--force` なし) で削除します。この途中で何か見つかったときは、tmux session は終了済み (キャッシュを消した後なら、キャッシュも削除済み) ですが、worktree と登録は残します。
  - Docker container が bind mount で worktree を使っている場合は検出できません。container を止めてから閉じてください。
  - 以前の版が既定の tmux server で起動したセッションがまだ動いているときもエラーにします ([README の tmux server の分離](README.md#tmux-server-の分離))。
  - Claude Code が作業中 (`BUSY`) でも確認せずに終了します。`cloud-coder status` で確かめてから閉じてください。
  - main checkout (`cc-REPO-1` の `~/git/REPO`) は削除しません。Claude Code の会話の履歴 (`~/.claude`) も残りますが、セッションの登録を外すので `connect --session` での再開はできなくなります。
  - VM が止まっていれば起動します。
  - 閉じたセッションの branch が remote などに残っている間は、その名前 (`cc-REPO-N`) を新しいセッションに使いません (古い branch や pull request の上で新しいタスクを始めないため)。

## スマホなどから Remote Control で操作する

cloud-coder が起動する Claude Code は、すべて Remote Control 付き (`--remote-control <セッション名>`) です。claude.ai や Claude アプリから、セッション名 (`cc-REPO-N`) で同じ Claude Code を操作できます。

- 使えるのは VM が動いていて、そのセッションの Claude Code が起動しているときだけです。VM を起動したり新しいセッションを作ったりはスマホからはできないので、ローカルで `connect` (例: `connect REPO -p "..." --detach`) してから、外出先で続きを見る使い方になります。
- VM 停止後は、`connect` したセッションの Claude Code だけが resume されます。Remote Control で使いたいセッションには一度 `connect` (`--detach` で可) してください。
- Remote Control から指示が来ている間も、応答を終えて入力待ちになれば idle です。応答の約 1〜2 分後から idle とみなされ、grace period (既定 10 分) の後に VM が止まります。考えながらゆっくり指示を出すなら `vm.idle_grace_minutes` を長めにしてください。
- Remote Control に必要なプランは [README の必要なもの](README.md#必要なもの) を参照してください。

## MCP server として使う

`cloud-coder mcp` は、VM の起動、タスクの投入、進み具合や結果の確認を tool として公開する stdio の MCP server です。tool の一覧は [README の MCP server](README.md#mcp-server) にあります。

- 対象の VM は `config.yaml` (と `cloud-coder mcp` に付けたオプション) で決まります。`gcp.project` が無いと server は起動せず、エラーになります。
- 典型的な流れは `up` (ready になるまで間をおいて呼び直す) → `start_session` (`repo` と `prompt`) → 開始したことをユーザーに報告してターンを終える → 後でユーザーが進み具合や結果を尋ねたら `read_session` / `status` で確認する → 必要なら `send_prompt` で追加の指示、です。作業が終われば VM は自動停止するので、`stop` を呼ぶ必要は普段ありません。
- Claude Code の作業は数分〜数時間かかります。server は LLM に、作業を始めたらターンを終え、`BUSY` が終わるのをポーリングで待たないよう指示します (`start_session` / `send_prompt` の応答の `next`、`BUSY` のときの応答の `note`)。`BUSY` と分かってから 60 秒以内に同じ `status` / `read_session` を呼ぶと、VM に問い合わせずに `rechecked: false` だけが返ります ([README の MCP server](README.md#mcp-server))。
- `up` は agent のインストール・更新を VM 上で始めてすぐに返り、終わるまでは `ready: false` (`agent_installing: true`) を返します。初回は数分かかります。失敗すると、次の `up` がエラーを返すと同時にインストールをやり直します (失敗したときのログは VM の `~/cloud-coder-install.log.prev`)。CLI の `up` / `connect` は、VM 上で動いているインストールの終了を待ってから続けます。
- `read_session` は tmux の画面の文字列をそのまま返します。Claude Code の応答のほか、権限の確認や trust 画面など入力を待っている表示もそのまま読めます。
- `send_prompt` は Claude Code が `BUSY` でも送れます。Claude Code のキューに入り、次の区切りで取り込まれます (応答の `prompt` が `queued`)。ダイアログの表示中はエラーになります (CLI の `-p` と同じ)。
- VM 上で行う初回の Claude Code のログイン (`/login`) と `gh auth login` は MCP からはできません。[初回セットアップ](#初回セットアップ)を CLI で済ませてから使ってください。

### MCP Inspector で tool を直接呼ぶ

試すだけなら、どこにも登録せずに [MCP Inspector](https://github.com/modelcontextprotocol/inspector) の CLI モードで tool を 1 回ずつ呼べます。Inspector は呼び出しごとに `cloud-coder mcp` を起動し、結果を JSON で表示して終了します。Node.js 22.19 以上 (`npx`) が必要です。

```bash
npx @modelcontextprotocol/inspector --cli cloud-coder mcp --method tools/list
npx @modelcontextprotocol/inspector --cli cloud-coder mcp --method tools/call --tool-name status
```

引数のある tool は `--tool-arg key=value` を引数ごとに付けます。

```bash
npx @modelcontextprotocol/inspector --cli cloud-coder mcp \
  --method tools/call --tool-name read_session --tool-arg session=cc-REPO-1 --tool-arg lines=50
```

- `status` と `read_session` は VM を起動しません。`up` / `start_session` / `send_prompt` は VM を起動し、`stop` は止めます。
- tool がエラーを返すと、Inspector は終了コード 0 以外で終わります (例: VM が止まっているときの `read_session`)。
- `cloud-coder mcp` にオプションを付けるときは、server のコマンドの後に `--` を置き、Inspector のオプションをその後ろに書きます (Inspector CLI では `--` より前が server のコマンドです。`--config` は Inspector 自身のオプションとも重なります)。

```bash
npx @modelcontextprotocol/inspector --cli cloud-coder mcp --config ~/.config/cloud-coder/config.yaml \
  -- --method tools/call --tool-name status
```

オプションの詳細は [Inspector CLI の README](https://github.com/modelcontextprotocol/inspector/blob/main/clients/cli/README.md) と [MCP server configuration](https://github.com/modelcontextprotocol/inspector/blob/main/docs/mcp-server-configuration.md#the----separator) を参照してください。

### Claude Code に登録する (任意)

Claude Code などのエージェントに tool として使わせる場合は登録します。scope を指定しない `claude mcp add` は local scope で、実行したディレクトリ (プロジェクト) でだけ読み込まれます。使いたいディレクトリで実行してください。

```bash
claude mcp add cloud-coder -- cloud-coder mcp
claude mcp list   # cloud-coder が Connected になっていること
```

- `--scope user` を付けると、そのマシンのすべてのプロジェクトで読み込まれます。scope の違いは [Claude Code のドキュメント](https://code.claude.com/docs/en/mcp#mcp-installation-scopes) を参照してください。
- 登録をやめるときは `claude mcp remove cloud-coder` を、同じディレクトリで実行します。

## HTTP API として使う

`cloud-coder api` は、MCP server を HTTP (`/mcp`、Streamable HTTP) で提供します。stdio で `cloud-coder mcp` を起動できないクライアント (ChatGPT) から同じ tool を使えます。`/mcp` は OAuth の access token だけで守られ、承認には allowlist の Google アカウントでのログインが要ります。環境変数、OAuth、セキュリティの注意は [README の HTTP API](README.md#http-api) に、設計は [docs/mcp-oauth.md](docs/mcp-oauth.md) にあります。

- 使い方は stdio の MCP server と同じです: `up` で VM を ready にする → `start_session` → 時間をおいて `status` / `read_session` → `send_prompt` で追加の指示。作業が終われば VM は自動停止します。
- 別の端末の Claude Code やスクリプトから使うときは、HTTP API ではなく、その端末に cloud-coder を入れて stdio の MCP server を登録してください ([Claude Code に登録する](#claude-code-に登録する-任意))。gcloud の認証と IAP で VM に届きます。

### Cloud Run に置いてどこからでも使う

[infra/README.md](infra/README.md) の Terraform で、API を Cloud Run に公開できます。構築には Google の OAuth client と、署名鍵・client secret の Secret が要ります (手順は infra/README.md)。

- endpoint はインターネットに公開され、allowlist の Google アカウントが承認した OAuth grant だけで守られます。
- Cloud Run 上では `/healthz` に届きません (Cloud Run の予約パス)。生存確認は token なしで `curl -s "${MCP_URL%/mcp}/.well-known/oauth-authorization-server"` のように OAuth の metadata を読むか、ChatGPT から `status` tool を呼びます。
- 1 回の呼び出しに数秒かかります (IAP 経由の SSH)。

### ChatGPT から使う

Cloud Run に置いた API は `/mcp` で MCP server も提供しているので、ChatGPT の developer mode のアプリとして登録できます。仕組みと注意は [README の HTTP API](README.md#http-api) にあります。

1. MCP の URL と client secret を手元に用意する。
   ```bash
   terraform -chdir=infra output -raw mcp_url   # https://cloud-coder-api-<project number>.<region>.run.app/mcp
   gcloud secrets versions access latest --secret cloud-coder-api-oauth-client-secret --project <project>
   ```
   URL は `terraform output url` (`…a.run.app`) ではなく `mcp_url` の方を使います。OAuth の issuer と resource がこの URL だからです。
2. ChatGPT (web) の **Settings → Security and login** で **Developer mode** を有効にする。
3. ChatGPT の Apps (Plugins) の画面で **+** (Create) を押し、次のように入力して作成する。
   - Name: 任意 (例: cloud-coder)
   - MCP Server URL: 手順 1 の `mcp_url`
   - Authentication: **OAuth**。この server は Dynamic Client Registration を提供しないので、client を手で入力します。
     - Client ID: `cloud-coder`
     - Client secret: 手順 1 の値
     - トークンエンドポイントの認証方式 (token endpoint auth method): `client_secret_post` (既定が `none` なら変える)
   - コールバック (redirect) URL が表示されたら `https://chatgpt.com/connector_platform_oauth_redirect` であることを確かめる。
4. ChatGPT が cloud-coder の同意画面 (client、付与する権限、受け取り先の URL が出るページ) を開く。受け取り先が `https://chatgpt.com/connector_platform_oauth_redirect` であることを確かめ、**Continue with Google** を押して、allowlist に入れた Google アカウントでログインする。ChatGPT に戻れば接続完了です。
   - 「This Google account may not approve access」と出たら、表示された数字 (`sub`) を Terraform の変数 `oauth_allowed_subs` に入れて apply し、手順 4 をやり直します (初回はこれで自分の `sub` を知ります)。
5. 会話でアプリを選び、「cloud-coder の status を見て」のように頼む。`up` / `start_session` / `send_prompt` / `stop` は書き込みの tool なので、ChatGPT が実行前に確認を求めます。
6. 作業を頼むと (例: 「cloud-coder で PR を作って」)、ChatGPT は `start_session` の後に開始したことを伝えてターンを終えます。結果は後で「進み具合を見て」「結果を教えて」のように尋ねてください。

- 接続は access token (1 時間) を ChatGPT が refresh して続きます。承認から 30 日たつと切れるので、アプリの接続をやり直してください (手順 4)。
- 同意画面で Continue を押すのは、自分で ChatGPT から接続を始めた直後に開いたページだけにしてください。
- client secret か署名鍵を入れ替えた場合、または自分の `sub` を allowlist から外した場合も、接続をやり直します (client secret を変えたら手順 3 の値も更新)。
- ChatGPT が書き込みの tool を呼べるかはプランによります。OpenAI の developer mode のドキュメントは Plus / Pro でも書き込みを許可 (実行前に確認) としていますが、他のページでは制限がある書き方もあるので、呼べない場合はプランの制限を確認してください。

## Claude Code のスキルで操作する

このリポジトリの [`.claude/skills/cloud-coder/SKILL.md`](.claude/skills/cloud-coder/SKILL.md) は Claude Code の [project skill](https://code.claude.com/docs/en/skills#where-skills-live) です。インストールは不要で、このリポジトリ (と、その git worktree) で起動した Claude Code でだけ使えます。

```text
/cloud-coder status
/cloud-coder up
/cloud-coder https://github.com/OWNER/REPO.git テストが落ちている原因を調べて直して
/cloud-coder cc-REPO-1 に「修正を PR にして」と送って
/cloud-coder read cc-REPO-1
/cloud-coder stop
```

- [初回セットアップ](#初回セットアップ)の 2〜5 (gcloud の認証、config.yaml、`gh auth login`、Claude Code の `/login`) を済ませてから使います。CLI は `uv run cloud-coder` で、このチェックアウトのものが使われます (`uv tool install` は不要です)。画面の読み取りには MCP Inspector (バージョン固定) を `npx` で使うので、Node.js 22.19 以上が必要です。
- スキルは Sonnet で動きます (frontmatter の `model: sonnet`)。モデルの切り替えはそのターンだけで、次に入力したときは元のモデルに戻ります。
- `/cloud-coder` と打ったときだけ動きます (`disable-model-invocation: true`)。このリポジトリでは cloud-coder 自体の開発で status や stop の話が頻繁に出るため、Claude が会話から判断して VM を起動・停止しないようにしています。
- タスクは `connect --detach` で渡すだけで、結果は待ちません。後で `/cloud-coder read <セッション名>` で画面を読みます。VM が止まっていれば、読む前に起動するかを聞きます。
- 感嘆符で始まる指示 (Claude Code の shell モード) は送りません。対象の VM を変えるオプション (`--project` など) も付けません。
- VM を止めるのは `stop` を頼んだときだけです。作業を渡した後は自動停止に任せます。`stop` のときは先に status を見て、作業中 (`BUSY`) のセッションがあれば止めずに確認を求めます。
- status・起動・タスクの投入・画面の読み取りのコマンドは、スキルを呼んだターンの間だけ許可なしで実行されます (frontmatter の `allowed-tools`)。`stop` は許可を求めます。許可ルールはコマンドの先頭部分だけを見るので、オプションの追加 (`--project`、Inspector の `--tool-name` の上書きなど) までは防げません。それらはスキルの指示で禁じています。

## 自動停止を使いこなす

VM 上で 1 分ごとに判定が走り、VM 全体が idle になった状態が grace period (既定 10 分) 続くと VM を停止します。判定は起動の 2 分後から始まります。詳しい判定規則は [README の自動停止](README.md#自動停止) にあります。

### 止まらない (busy 扱いになる) もの

| 状況 | 補足 |
| --- | --- |
| Claude Code が作業中 (`BUSY`) | プロンプトを送ってから応答を終えるまで |
| Claude Code に background task やセッション内の cron が残っている | `run_in_background` の shell などが動いている間は応答後も `BUSY` |
| Claude Code がまだ状態を報告していない | 起動直後、ログイン画面や trust 画面のまま |
| tmux の pane でコマンドが動いている | フォアグラウンドのコマンドも、シェル配下のバックグラウンドジョブも (`npm run dev` など) |
| Docker コンテナが動いている | tmux の外の `docker compose up -d` なども |
| tmux の外で SSH ログインしている | シェルのプロンプト待ちのまま 30 分入力が無いログインは数えない。コマンドが動いていれば入力が無くても busy |
| cloud-coder の `connect` / agent のインストール中 | |

逆に、次は idle とみなされます。

- 応答を終えて入力待ちの Claude Code (応答から約 1〜2 分後)
- 新しく起動しただけで、まだプロンプトを送っていない Claude Code
- resume しただけでイベントが無いまま 10 分経った Claude Code
- プロンプト待ちのシェルだけの pane、`tmux attach` しているだけの SSH

### いつ止まるか

1. 上の busy 要因がすべて無くなる
2. 次の判定 (1 分以内) で grace period が始まる (`status` に `shutdown in ...s` が出る)
3. grace period の間に何か始まれば (プロンプト送信、`connect` など) 取り消される
4. grace period が終わった時点の判定でもまだ idle なら停止する

### 設定で調整する

```yaml
vm:
  idle_grace_minutes: 30         # 猶予を延ばす
  ignore_docker: true            # 動いているコンテナがあっても止める
  ignore_ssh_sessions: true      # SSH ログインがあっても止める
  ssh_session_idle_minutes: 60   # 入力の無い SSH ログインを数える時間
```

次の `up` / `connect` で反映されます。自動停止そのものを無効にするキーはありません。一時的に止めたくないときは、tmux の pane でコマンドを動かしておけば busy になります (例: 新しい window で `sleep 4h`)。終わったら `Ctrl-c` するのを忘れないでください。

## 停止した VM で作業を再開する

```bash
cloud-coder connect REPO     # または connect / connect --session cc-REPO-N
```

- VM を起動し、tmux session を作り直して、`claude --resume <session-id>` で同じ会話を再開します。会話の対応表と Claude Code の履歴は Persistent Disk にあるので停止しても消えません。
- `/clear` などで会話が切り替わっていた場合も、最後の会話に戻ります。
- 再開されるのは `connect` したセッションだけです。他のセッションはそれぞれ `connect` したときに再開されます。
- tmux の中で動かしていた他のプロセス (dev server など) は再開されません。
- 続きの指示を渡すときは `connect --session cc-REPO-N -p "..."` とします。`connect REPO -p "..."` は新しいセッションで始めます。
- 自動停止は idle のときにしか起きませんが、`cloud-coder stop` した時点で動いていた作業は中断されています。必要なら再開した会話で続きを指示してください。
- resume しただけで何もしなければ、10 分 + grace period で再び停止します。

## マシンスペックを変える

```bash
cloud-coder stop
cloud-coder connect REPO --machine-type t2d-standard-16   # 停止中なら変更してから起動する
```

- 既存の VM の machine type を変えるのは `--machine-type` を指定したときだけです。config.yaml の `gcp.machine_type` を書き換えても、既存の VM には反映されません (新しく作る VM に使われます)。今後も同じ machine type で作りたいなら config も合わせて書き換えてください。
- VM が動いている間に `--machine-type` を指定しても変更されず、`cloud-coder stop` の後に適用する旨のメッセージが出ます。指定は記録されないので、`stop` した後にもう一度 `--machine-type` を付けて `connect` / `up` してください。
- `status` の 1 行目で現在の machine type を確認できます。
- disk のサイズや種類は VM 作成時にだけ使われ、cloud-coder は既存 disk を変更しません。
- どの machine type を選ぶかは [README のマシンタイプの目安](README.md#マシンタイプの目安) (既定の t2d-standard-8、安価な E2、重い用途の 16 vCPU など) を参照してください。小さい VM で複数 agent を動かすときは [README の運用](README.md#小さい-vm-で複数-agent-を動かすときの運用) も役立ちます。

## トラブルシューティングと注意点

### 止まらないときに確認する

`cloud-coder status` の `auto-stop: blocked` の下に理由が出ます。

| 表示 | 対処 |
| --- | --- |
| `claude cc-X-1 (%0) is BUSY` | 作業中なら待つ。作業していないのに残っている場合は下の「Esc で中断した後」を参照 |
| `claude cc-X-1 (%0) is READY` | 応答直後。約 1〜2 分で idle になります |
| `cc-X-1 %0: claude pid N has not reported state yet` | ログイン画面や trust 画面で止まっている可能性。attach して確認する。ログイン済みでも消えない場合は、組織の managed settings で cloud-coder の hook が読まれていない可能性があります ([README の自動停止](README.md#自動停止)) |
| `cc-X-1 %1: running node` | その pane でコマンドが動いている。不要なら止める |
| `cc-X-1 %1: shell has running processes ...` | シェルのバックグラウンドジョブが残っている。`jobs` で確認して止める |
| `docker: running containers ...` | `docker compose down` などで止めるか、`vm.ignore_docker: true` |
| `ssh login coder pts/1 from ... (no input for N min)` | tmux の外の SSH ログインが残っている。`exit` するか、30 分入力が無ければ数えられなくなる |
| `ssh login coder pts/1: running ...` | tmux の外の SSH ログインでコマンドが動いている。入力が無くても数えられ続けるので、止めて `exit` する |
| `cloud-coder launch in progress` / `cloud-coder install in progress` | `connect` / agent のインストールの処理中。終われば消えます |
| `docker could not be queried` / `tmux server could not be queried` | 問い合わせが失敗したため安全側で busy。続くなら VM に入って `docker ps` / `tmux -L cloud-coder ls` を確認する |

VM 上のログは、VM に SSH して `journalctl -u cloud-coder-idle-check.service` (毎分の判定。grace period の開始時と停止時には idle と判断した根拠も出ます) と `journalctl -t cloud-coder-hook` (hook のイベントごとの状態の変化) で見られます。VM が止まった後も残ります。

### Esc で中断した後は BUSY が残る

`Esc` でターンを中断すると Claude Code の hook が発火しないため、次のプロンプトを送るまで `BUSY` のままになり、VM は止まりません。何かプロンプトを送って応答を終わらせるか、不要なら Claude Code を終了 (`/exit`) してください。

### resume した後は 10 分 + grace period 止まらない

停止した VM に `connect` して会話を見るだけでも、resume した Claude Code はイベントが無いまま 10 分経つまで busy 扱いです。その後 grace period を経て停止します。すぐ止めたいなら `cloud-coder stop` します。

### trust 画面が出る

cloud-coder は、自分の clone 先 / worktree 先のディレクトリで、clone の `origin` がセッションの URL と一致することを確認できた場合だけ trust を自動承認します。次の場合は Claude Code の trust 画面が出るので、attach して答えてください。

- URL を持たないセッション (手で clone したリポジトリに、URL を渡さず初めて `connect REPO` した場合など)
- clone の `origin` を後から変えた場合
- `claude.auto_trust_workspace: false` にしている場合

前の 2 つは `connect` が `workspace trust skipped: ...` と警告します。

`--detach` で投げたセッションが trust 画面で止まると、作業が始まらず VM も止まりません。

### private repository の clone に失敗する

`git clone ... failed` のエラーになったら、次を確認します。

- VM で `gh auth login` と `gh auth setup-git` を済ませたか ([初回セットアップ](#4-vm-を作り-github-を認証する))
- fine-grained token の場合、そのリポジトリが token の対象に入っているか
- `git.github_https: false` にしている場合、SSH URL の clone はローカルの ssh-agent の転送 (`connect` の間だけ) に頼ります。鍵を `ssh-add` しているか

dotfiles リポジトリの clone 失敗は警告だけで先へ進み、次の `up` / `connect` で再試行されます。

### docker が permission denied になる

`coder` は `docker` グループに追加されますが、グループは新しいログインから有効で、tmux の pane は tmux server のグループを引き継ぎます。docker を入れた時点で既に tmux server が動いていた場合 (後から `vm.tools` に docker を足したときなど) に起きます。初回の `up` → `connect` では起きません。

- cloud-coder が新しく作る tmux session と window では sudo なしで使えるようにしています (`connect REPO --new` など)。
- docker を入れる前からある pane や、自分で開いた window (`Ctrl-b c`) では使えません。SSH で入り直しても既存の tmux server は変わらないので、`cloud-coder stop` してから `connect` し直すのが確実です ([README の開発ツール](README.md#開発ツール))。

### 課金の注意

- VM が停止していても Persistent Disk (既定 100 GB の SSD) は課金され続けます。使わなくなったら[片付け](#片付ける)てください。
- `connect` で起動したまま、busy 要因 (dev server、コンテナ、Esc で中断した Claude Code、閉じ忘れた SSH でコマンドが動いているものなど) が残っていると、VM は止まらず課金が続きます。離れる前に `cloud-coder status` で `auto-stop` を確認するのが確実です。
- 料金は [GCP 料金計算ツール](https://cloud.google.com/products/calculator)で確認してください。

## 片付ける

cloud-coder には VM や disk を削除するコマンドはありません。gcloud で削除します。

```bash
gcloud compute instances delete cloud-coder --project <project> --zone asia-northeast1-b
```

- VM 作成時の boot disk は VM と一緒に削除されます。VM 上の clone、worktree、Claude Code の履歴や認証情報もすべて消えるので、push していない変更が無いか先に確認してください。
- disk を残したい場合は `--keep-disks=boot` を付けます (残した disk は課金され続けます)。
- cloud-coder 自体を消すには `uv tool uninstall cloud-coder`、設定は `~/.config/cloud-coder/` を削除します。
