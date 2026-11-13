# agent-sessidx

AI コーディングエージェントの過去セッションを、「何を頼んで何をしたか」で検索するローカル CLI とスキルです。Claude Code の transcript（`~/.claude/projects/*/*.jsonl`）と Codex の rollout（`$CODEX_HOME/sessions/` と `$CODEX_HOME/archived_sessions/` の下の `rollout-*.jsonl` と `rollout-*.jsonl.zst`。`CODEX_HOME` が未設定なら `~/.codex`）に対応しています。Codex が終わったセッションを圧縮した `.zst` を読むには、Python 3.14 以上か `zstd` コマンドが要ります。どちらも無いと `.zst` は索引に入らず、`stats` がその本数を出します。

エージェントは作業の途中で自分から検索し、過去のセッションの依頼・触ったファイル・PR・最後の返答を読んで文脈をつかみます。要約に LLM は使いません。transcript から抜き出すだけなので、今進行中のセッションも次の検索から見つかります。

## 必要なもの

- Python 3.9 以上（標準ライブラリだけで動きます）
- macOS か Linux（索引の排他に `fcntl` を使います）

## インストール

### Claude Code のプラグインとして入れる

Claude Code の中で次の2行を実行します。

```
/plugin marketplace add nakashidev-user/agent-sessidx
/plugin install agent-sessidx@agent-sessidx
```

Claude Code 2.1.275 以降なら、`/plugin install agent-sessidx --marketplace nakashidev-user/agent-sessidx` の1行でも入ります。

スキルは `agent-sessidx:agent-sessidx` という名前で入ります。CLI はプラグインの置き場所（`~/.claude/plugins/cache/agent-sessidx/agent-sessidx/<version>/agent-sessidx.py`）にあり、スキルはそこを呼びます。索引は最初の検索で作られます。

### clone して `install.sh` で入れる

```bash
git clone https://github.com/nakashidev-user/agent-sessidx.git
cd agent-sessidx
./install.sh
```

`install.sh` は次のシンボリックリンクを作り、最後に索引を作ります。同じ名前のファイルやディレクトリがすでにあるときは、何も上書きせずに止まります。clone した作業ツリーをそのまま使うので、手元で CLI を直しながら試すときはこちらが向いています。プラグインと両方入れると、同じスキルが2つ並ぶのでどちらか1つにしてください。`install.sh` からプラグインへ移るときは、`~/.claude/tools/agent-sessidx` と `~/.claude/skills/agent-sessidx` の2つのリンクを外してからプラグインを入れます。

| リンク | 指す先 |
|---|---|
| `~/.claude/tools/agent-sessidx` | このリポジトリ |
| `~/.claude/skills/agent-sessidx` | `skills/agent-sessidx` |
| `${CODEX_HOME:-~/.codex}/skills/agent-sessidx`（そのディレクトリがあるときだけ） | `skills/agent-sessidx` |

Codex から使うときも `install.sh` で入れます。Codex のホームがあれば、Codex のスキルとしてもリンクします（Codex はシンボリックリンクのスキルも読みます）。

### Codex のセッションで索引に入るもの

| Claude Code | Codex |
|---|---|
| カスタムタイトル、AI タイトル | `session_index.jsonl` のスレッド名（最後の行が今の名前） |
| ユーザーのプロンプト | `UserMessage`（古い形式は `user_message`）。IDE などの文脈ブロックの後の `## My request for Codex:` より後ろだけ |
| 最後の返答 | 最後の `AgentMessage`（古い形式は `agent_message`） |
| pr-link | 返答に出てきた `https://github.com/<owner>/<repo>/pull/<番号>` |
| 読み書きしたファイル | `FileChange` で変えたファイル |
| プロジェクトのディレクトリ名 | cwd の英数字以外を `-` にしたもの（Claude Code と同じ名前になる） |

他のエージェントから取り込んだスレッド（`external-import-turn-` の依頼で始まるもの）とサブエージェントのスレッドは索引に入れません。ツールの呼び出しと出力、AGENTS.md などの注入、圧縮の履歴も入れません。スレッドの名前を変えても、検索の重みに反映されるのは rollout が変わるか `rebuild` したときです（`show` には今の名前が出ます）。

## 使い方

次の例は `install.sh` で入れたときのパスです。プラグインで入れたときは `~/.claude/tools/agent-sessidx/` をプラグインの置き場所に読み替えてください。

```bash
python3 -I ~/.claude/tools/agent-sessidx/agent-sessidx.py search 請求書 再発行
python3 -I ~/.claude/tools/agent-sessidx/agent-sessidx.py show <session-id の先頭> [--prompts 30]
python3 -I ~/.claude/tools/agent-sessidx/agent-sessidx.py stats
python3 -I ~/.claude/tools/agent-sessidx/agent-sessidx.py rebuild
```

| オプション | 意味 |
|---|---|
| `-n 8` | 表示する件数 |
| `-p <文字列>` | プロジェクト名か cwd にこの文字列を含むセッションに絞る |
| `--days N` | N 日以内に動いたセッションに絞る |
| `--agent claude` / `--agent codex` | そのエージェントのセッションに絞る |
| `--auto` | `<scheduled-task>` で始まる定期実行のセッションも含める |
| `--include-self` | 呼び出し元のセッションも結果に残す（呼び出し元は Claude Code の `$CLAUDE_CODE_SESSION_ID` と Codex の `$CODEX_THREAD_ID` で見分ける） |

Claude Code からは、スキル `agent-sessidx`（プラグインで入れたときは `agent-sessidx:agent-sessidx`）として「前にやった？」「前回どうした？」のような場面で呼ばれます。

## 索引について

- 置き場所は `~/.claude/agent-sessidx/index-v<VERSION>.bin`（今は `index-v9.bin`）で、このリポジトリの外です。ファイル名に索引の版が入るので、プラグイン版と install.sh 版のように版の違うものが同じマシンにあっても、互いの索引を上書きせず、切り替えるたびに全件を作り直すこともありません。古い版が使う `index.bin` などはそのまま残します（要らなければ手で消してかまいません）。
- **索引には全セッションのタイトル・依頼の本文・サブエージェントへの委任文・最後の返答・ファイルパス・cwd・ブランチ・PR URL・transcript のパスが入ります。** 共有したりコミットしたりしないでください。`.gitignore` でも `*.bin` を除外しています。
- 検索のたびに transcript の更新時刻とサイズを見て、変わったものだけ読み直します。ただし要約用コピーとして索引に載っている transcript は、コピーかどうかが最初の依頼だけで決まり後から変わらないので、更新時刻もサイズも見ません（一覧から消えたら索引からも消します）。読もうとしたときに消えていた transcript は索引に残さず、次の検索でもう一度確かめます。
- `--include-self` なしの `search` では、呼び出し元のセッション（`$CLAUDE_CODE_SESSION_ID` か `$CODEX_THREAD_ID`）の transcript は変わっていても読み直しません。どうせ結果から外すので、結果は変わりません。読み直しと索引の書き直しは、そのセッションを外さない次の呼び出し（他のセッションからの `search`、`--include-self` 付きの `search`、`show`、`stats`、`rebuild`）に回します。ほかに変わった transcript が無ければ、索引ファイルも書き直しません。
- 作り直しにかかる時間は、手元の約7,000 transcript（5GB 超）で約5秒でした。

## 検索の仕組み

- 転置インデックスです。日本語は2文字ずつ（bigram）、英数字は単語単位で区切ります。snake_case と camelCase は部品にも分けます。
- 順位は BM25（k1 1.2、b 0.5）です。その点数に、検索語の何割が当たったか、セッションの往復数、新しさの3つを掛けます。
- 重みは、タイトル 4、最初の依頼 2、ブランチ・プロジェクト・PR 2、その他の依頼・作業中に送った依頼・サブエージェントへの委任文 1、最後の返答 1、ファイルパス 1 です。
- 「最初の依頼」は、引数のないスラッシュコマンド（`/model` や `/list-skills` のように、コマンド名だけのもの）を飛ばした最初の依頼です。検索結果の `first:` も重み 2 もこれに付きます。飛ばしたコマンドも依頼の一つとして重み 1 で索引に入り、`show` の prompts にも出ます。コマンドしか無いセッションでは、そのコマンドを最初の依頼にします。
- `<transcript>` で始まるセッション（他のセッションを要約するために作られたコピー）は索引に入れません。中にスラッシュコマンドを含むコピーも同じです。コピーかどうかは最初の依頼だけで決まるので、索引を作るときはそこで読むのをやめ、残りの行は読みません。

## テスト

```bash
python3 -I -m unittest discover -s tests -v
```

テストはその場で作った架空の transcript だけを使います。本物の transcript をこのリポジトリに入れないでください。

## 制約

- 対応しているのは Claude Code と Codex です。エージェントを足すときは、`SOURCES` に `list`（transcript のパスを返す）と `parse`（1本を `new_session` の形に読む）を加えます。`self_env`（呼び出し元のセッション ID が入る環境変数）と `resume`（再開コマンドの書式）、`index_parse`（索引作り用の軽い `parse`。要約用コピーを途中で読みやめるなど）は任意です。
- 文字で照合するので、言い換えには弱いです（例: 「アンケート」と「survey」）。
- 索引に入るのは、依頼・委任文・最後の返答・ファイルパスです。会話途中の返答は入りません。
