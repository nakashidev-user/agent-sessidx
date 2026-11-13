import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("agent_sessidx", ROOT / "agent-sessidx.py")
S = importlib.util.module_from_spec(spec)
sys.modules["agent_sessidx"] = S
spec.loader.exec_module(S)

TS = "2026-11-01T10:00:00.000Z"


def line(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def user(text, **extra):
    return line({"type": "user", "isSidechain": False, "timestamp": TS, "cwd": "/work/shop", "gitBranch": "feature/invoice",
                 "entrypoint": "cli", "message": {"role": "user", "content": text}, **extra})


def tool_result(text):
    return line({"type": "user", "timestamp": TS, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": text}]}})


def assistant(text="", file_path=None, brief=None):
    content = [{"type": "text", "text": text}] if text else []
    if file_path:
        content.append({"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": file_path}})
    if brief:
        content.append({"type": "tool_use", "id": "t2", "name": "Agent", "input": {"prompt": brief}})
    return line({"type": "assistant", "timestamp": TS, "message": {"role": "assistant", "content": content}})


def title(text):
    return line({"type": "ai-title", "aiTitle": text})


def pr(number):
    return line({"type": "pr-link", "prNumber": number, "prUrl": f"https://github.com/example/shop/pull/{number}", "timestamp": TS})


def queued(text):
    return line({"type": "attachment", "timestamp": TS, "attachment": {"type": "queued_command", "prompt": text}})


CX = "01a1c0de-0001-7000-8000-000000000001"


def codex(kind, payload):
    return line({"timestamp": TS, "type": kind, "payload": payload})


def codex_meta(sid, cwd="/work/shop", branch="feature/webhook-signature", source="cli"):
    return codex("session_meta", {"id": sid, "session_id": sid, "timestamp": TS, "cwd": cwd, "originator": "codex_cli_rs",
                                  "cli_version": "0.163.0", "source": source, "model_provider": "openai",
                                  "git": {"commit_hash": "e65c649", "branch": branch, "repository_url": "https://github.com/example/shop.git"}})


def codex_item(item, turn):
    return codex("event_msg", {"type": "item_completed", "thread_id": "t", "turn_id": turn, "item": item, "completed_at_ms": 0})


def codex_user(text, turn="turn-1"):
    return codex_item({"type": "UserMessage", "id": "item-1", "content": [{"type": "text", "text": text, "text_elements": []}]}, turn)


def codex_reply(text, turn="turn-1"):
    return codex_item({"type": "AgentMessage", "id": "item-2", "content": [{"type": "Text", "text": text}]}, turn)


def codex_edit(*paths):
    return codex_item({"type": "FileChange", "id": "call-1", "status": "completed",
                       "changes": {p: {"type": "update", "unified_diff": "@@ -1 +1 @@"} for p in paths}}, "turn-1")


def codex_traffic(text):
    """What Codex records besides the conversation: injected instructions, the raw model input, tool calls and output, compaction history."""
    return [
        codex("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": f"# AGENTS.md instructions\n\n<INSTRUCTIONS>\n{text}\n</INSTRUCTIONS>"},
            {"type": "input_text", "text": f"<environment_context>\n  <cwd>/work/shop</cwd>\n  {text}\n</environment_context>"}]}),
        codex("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"決済Webhookの署名検証を追加して {text}"}]}),
        codex("response_item", {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": f"rg {text}"}), "call_id": "c1"}),
        codex("response_item", {"type": "function_call_output", "call_id": "c1", "output": f"{text} が含まれるログ"}),
        codex_item({"type": "CommandExecution", "id": "c2", "command": ["/bin/zsh", "-lc", f"echo {text}"],
                    "aggregated_output": text, "exit_code": 0, "status": "completed"}, "turn-1"),
        codex("compacted", {"message": "", "replacement_history": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}]}),
    ]


def zstd_compress(data):
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd is not None:
        return zstd.compress(data)
    if shutil.which("zstd"):
        return subprocess.run(["zstd", "-q", "-c"], input=data, stdout=subprocess.PIPE, check=True).stdout
    return None


class SessionIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.projects = base / "projects"
        (self.projects / "-work-shop").mkdir(parents=True)
        self.codex_home = base / "codex"
        self.saved = (S.CLAUDE_PROJECTS, S.INDEX_DIR, S.INDEX_PATH, S.LOCK_PATH, S.CODEX_HOME)
        self.saved_thread = os.environ.pop("CODEX_THREAD_ID", None)
        S.CLAUDE_PROJECTS = str(self.projects)
        S.INDEX_DIR = str(base / "index")
        S.INDEX_PATH = os.path.join(S.INDEX_DIR, os.path.basename(self.saved[2]))
        S.LOCK_PATH = os.path.join(S.INDEX_DIR, "lock")
        S.CODEX_HOME = str(self.codex_home)

        self.write("aaaa1111", [
            title("請求書PDFの出力"),
            user("<command-name>/plan</command-name>\n<command-message>plan</command-message>\n<command-args>請求書の再発行フローを設計して</command-args>"),
            assistant("調べます", file_path="/work/shop/billing/invoice_renderer.go", brief="InvoiceRenderer の呼び出し元を洗い出して"),
            tool_result("ZZTOOLOUTPUTONLY が含まれるログ"),
            queued("宛名の敬称も直して"),
            user("This session is being continued ZZCOMPACTONLY", isCompactSummary=True),
            pr(4242),
            assistant("請求書の再発行フローを実装しました"),
        ])
        self.write("bbbb2222", [
            title("ログイン画面の合鍵リセット"),
            user("いいね機能のカウントがずれる件とパスワードリセット"),
            assistant("直しました", file_path="/work/shop/auth/reset.go"),
        ])

    def tearDown(self):
        S.CLAUDE_PROJECTS, S.INDEX_DIR, S.INDEX_PATH, S.LOCK_PATH, S.CODEX_HOME = self.saved
        os.environ.pop("CODEX_THREAD_ID", None)
        if self.saved_thread is not None:
            os.environ["CODEX_THREAD_ID"] = self.saved_thread
        self.tmp.cleanup()

    def write(self, session_id, lines, project="-work-shop", mode="w"):
        path = self.projects / project / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode, encoding="utf-8") as f:
            f.writelines(lines)
        return path

    def write_codex(self, session_id, lines, compressed=False, folder="sessions/2026/11/01"):
        data = "".join(lines).encode("utf-8")
        name = f"rollout-2026-11-01T10-00-00-{session_id}.jsonl"
        if compressed:
            data = zstd_compress(data)
            if data is None:
                self.skipTest("neither compression.zstd nor the zstd command is available")
            name += ".zst"
        path = self.codex_home / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def name_codex(self, session_id, name):
        self.codex_home.mkdir(parents=True, exist_ok=True)
        with open(self.codex_home / "session_index.jsonl", "a", encoding="utf-8") as f:
            f.write(line({"id": session_id, "thread_name": name, "updated_at": "2026-11-01T10:05:00Z"}))

    def write_webhook_session(self, compressed=False):
        self.name_codex(CX, "Stripe署名の改ざん対策")
        return self.write_codex(CX, [
            codex_meta(CX),
            *codex_traffic("ZZCODEXNOISE"),
            codex_user("# Context from my IDE setup:\n\n## Open tabs:\n- ZZOPENTAB.go: payments/ZZOPENTAB.go\n\n"
                       "## My request for Codex:\n決済Webhookの署名検証を追加して\n"),
            codex_reply("署名を検証している箇所を探します"),
            codex_edit("/work/shop/payments/webhook_verifier.go"),
            codex_user("リトライ時の冪等キーも確認して", turn="turn-2"),
            codex_reply("冪等キーの確認を入れて https://github.com/example/shop/pull/5151 を作りました", turn="turn-2"),
            codex_user("CIの結果も見て", turn="turn-3"),
            codex_reply("CIはすべて通りました", turn="turn-3"),
        ], compressed=compressed)

    def top_ids(self, query, **kw):
        return [m["id"] for _, m in S.search(S.refresh(), query, 5, **kw)]

    def test_each_kind_of_record_makes_the_session_findable(self):
        # The point of the index is recalling a session from whatever the user remembers about it.
        for query in ["請求書PDF", "再発行フロー", "敬称", "invoice_renderer", "呼び出し元", "4242"]:
            with self.subTest(query=query):
                self.assertEqual(self.top_ids(query)[:1], ["aaaa1111"])

    def test_tool_output_and_compaction_summaries_are_not_indexed(self):
        # Tool output and compaction summaries are bulk noise that would drown out what the user asked for.
        self.assertEqual(self.top_ids("ZZTOOLOUTPUTONLY"), [])
        self.assertEqual(self.top_ids("ZZCOMPACTONLY"), [])

    def test_summarizer_copies_of_other_sessions_are_skipped(self):
        # Copies would duplicate every real session and crowd it out of the top results.
        self.write("cccc3333", [user("<transcript>\nuser: 請求書の再発行フロー\n</transcript>")])
        self.assertEqual(self.top_ids("再発行フロー"), ["aaaa1111"])

    def test_summarizer_copies_that_open_with_a_slash_command_are_skipped(self):
        # Most real copies quote a session that began with a slash command, so the copy itself contains <command-name>.
        self.write("dddd4444", [user("<transcript>\nuser: <command-message>plan</command-message>\n<command-name>/plan</command-name>\n"
                                     "user: 請求書の再発行フロー\n</transcript>\n\n上記を要約してください")])
        self.assertEqual(self.top_ids("再発行フロー"), ["aaaa1111"])
        self.assertEqual(self.top_ids("plan"), ["aaaa1111"])

    def test_new_prompts_in_a_live_session_are_searchable_on_the_next_call(self):
        # Reading transcripts directly, with no summarizing step, is what makes the running session searchable at once.
        self.assertEqual(self.top_ids("消費税"), [])
        self.write("bbbb2222", [user("消費税の端数処理も確認して")], mode="a")
        self.assertEqual(self.top_ids("消費税"), ["bbbb2222"])
        self.assertEqual(S.refresh().live, 2)

    def test_the_calling_session_can_be_excluded(self):
        # Otherwise an agent cites its own conversation as past evidence.
        self.assertEqual(self.top_ids("請求書", exclude_ids={"aaaa1111"}), [])

    def test_japanese_single_kanji_and_hiragana_words(self):
        # A one-kanji query must still hit text indexed as bigrams, and a hiragana-only feature name must not be dropped as a particle.
        self.assertEqual(self.top_ids("鍵"), ["bbbb2222"])
        self.assertEqual(self.top_ids("いいね"), ["bbbb2222"])

    def test_known_ascii_words_are_matched_whole(self):
        # Splitting a known identifier into its parts would also match unrelated sessions that only share a part.
        self.assertEqual(self.top_ids("InvoiceRenderer"), ["aaaa1111"])
        self.assertEqual(S.query_terms("InvoiceRenderer", S.refresh().postings), ["invoicerenderer"])

    def test_a_corrupt_index_is_rebuilt(self):
        # A broken index file must never block searching.
        S.refresh()
        Path(S.INDEX_PATH).write_bytes(b"garbage")
        self.assertEqual(self.top_ids("合鍵"), ["bbbb2222"])

    def test_a_machine_without_any_sessions_still_works(self):
        # A fresh install on a machine that has never run the agent must not crash.
        S.CLAUDE_PROJECTS = str(Path(self.tmp.name) / "missing")
        self.assertEqual(S.refresh().live, 0)
        self.assertEqual(self.top_ids("請求書"), [])

    def test_a_new_agent_needs_only_list_and_parse(self):
        # The README promises that adding an agent means adding one SOURCES entry with two functions.
        sid = "11111111"
        def parse(path):
            s = S.new_session(path, "other", sid, "other")
            s["prompts"] = ["ZZOTHERAGENT の依頼"]
            return s
        S.SOURCES["other"] = {"list": lambda: [str(self.projects / "-work-shop" / "aaaa1111.jsonl")], "parse": parse}
        try:
            self.assertIn(sid, self.top_ids("ZZOTHERAGENT", exclude_ids=S.calling_session_ids()))
            out = io.StringIO()
            with redirect_stdout(out):
                S.show(S.refresh(), sid, 5)
            self.assertIn("ZZOTHERAGENT", out.getvalue())
        finally:
            del S.SOURCES["other"]

    def test_cli_search_leaves_out_the_calling_session(self):
        # The CLI is what agents actually run; it must read the caller's id from the environment and not crash.
        out = io.StringIO()
        argv, env = sys.argv, os.environ.get("CLAUDE_CODE_SESSION_ID")
        sys.argv = ["agent-sessidx", "search", "請求書"]
        os.environ["CLAUDE_CODE_SESSION_ID"] = "aaaa1111"
        try:
            with redirect_stdout(out):
                S.main()
        finally:
            sys.argv = argv
            if env is None:
                os.environ.pop("CLAUDE_CODE_SESSION_ID")
            else:
                os.environ["CLAUDE_CODE_SESSION_ID"] = env
        self.assertEqual(out.getvalue().strip(), "no match")

    def test_cli_records_each_search_and_show_in_the_usage_log(self):
        # The log is how the owner learns whether agents actually reach for the tool, so every lookup must leave a line, failed ones included.
        argv = sys.argv
        try:
            for args in (["search", "請求書"], ["show", "aaaa"], ["show", "zzzz"]):
                sys.argv = ["agent-sessidx", *args]
                with redirect_stdout(io.StringIO()):
                    try:
                        S.main()
                    except SystemExit:
                        pass
        finally:
            sys.argv = argv
        with open(os.path.join(S.INDEX_DIR, "usage.jsonl"), encoding="utf-8") as f:
            entries = [json.loads(x) for x in f]
        self.assertEqual([(e["cmd"], e["arg"], e["ok"]) for e in entries],
                         [("search", "請求書", True), ("show", "aaaa", True), ("show", "zzzz", False)])
        self.assertIn("aaaa1111", entries[0]["ids"])
        self.assertEqual(entries[1]["ids"], ["aaaa1111"])

    def test_an_index_left_by_another_install_is_not_overwritten(self):
        # The plugin and the install.sh checkout can be different versions; if they shared one file, each switch between them would rebuild the whole index.
        os.makedirs(S.INDEX_DIR, exist_ok=True)
        legacy = Path(S.INDEX_DIR) / "index.bin"
        legacy.write_bytes(b"index written by plugin 0.1.0")
        self.assertEqual(self.top_ids("合鍵"), ["bbbb2222"])
        self.assertEqual(legacy.read_bytes(), b"index written by plugin 0.1.0")

    def test_a_bare_slash_command_is_not_taken_as_the_first_request(self):
        # Sessions often open with /model or a skill name and no arguments; the request that follows is what the user remembers and what the result should show.
        self.write("eeee5555", [
            user("<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args></command-args>"),
            user("在庫引当のバッチが二重に走る件を直して"),
            assistant("直しました"),
        ])
        hits = S.search(S.refresh(), "在庫引当", 5)
        self.assertEqual([m["id"] for _, m in hits], ["eeee5555"])
        self.assertEqual(hits[0][1]["first"], "在庫引当のバッチが二重に走る件を直して")

    def test_a_session_of_only_a_bare_slash_command_keeps_it_as_the_first_request(self):
        # With nothing else asked, the command is still the only record of what the session was for.
        self.write("ffff6666", [user("<command-name>/list-skills</command-name>\n<command-message>list-skills</command-message>")])
        hits = S.search(S.refresh(), "list-skills", 5)
        self.assertEqual([(m["id"], m["first"]) for _, m in hits], [("ffff6666", "/list-skills")])

    def test_show_prints_an_extractive_summary(self):
        # show is the summary an agent reads before relying on a past session.
        S.refresh()
        out = io.StringIO()
        with redirect_stdout(out):
            S.show(S.refresh(), "aaaa", 30)
        text = out.getvalue()
        for expected in ["請求書PDFの出力", "/plan 請求書の再発行フローを設計して", "宛名の敬称も直して",
                         "invoice_renderer.go", "InvoiceRenderer の呼び出し元", "pull/4242", "実装しました"]:
            self.assertIn(expected, text)
        self.assertNotIn("ZZTOOLOUTPUTONLY", text)

    def test_codex_sessions_are_searchable_by_what_was_asked_and_done(self):
        # Work done in Codex has to be recallable from the same things as work done in Claude Code: title, requests, files, PRs, branch.
        self.write_webhook_session()
        for query in ["Stripe", "署名検証", "冪等キー", "webhook_verifier", "5151", "signature"]:
            with self.subTest(query=query):
                self.assertEqual(self.top_ids(query)[:1], [CX])
        _, hit = S.search(S.refresh(), "署名検証", 5)[0]
        self.assertEqual((hit["agent"], hit["first"]), ("codex", "決済Webhookの署名検証を追加して"))
        self.assertEqual(hit["prs"], ["https://github.com/example/shop/pull/5151"])

    def test_codex_context_blocks_and_tool_traffic_are_not_indexed(self):
        # Injected instructions, IDE context, tool output and compaction history would drown out what the user asked for.
        self.write_webhook_session()
        self.assertEqual(self.top_ids("ZZCODEXNOISE"), [])
        self.assertEqual(self.top_ids("ZZOPENTAB"), [])

    def test_compressed_codex_rollouts_are_searchable(self):
        # Codex compresses finished rollouts to .jsonl.zst, so almost all past Codex work is in that form.
        self.write_webhook_session(compressed=True)
        self.assertEqual(self.top_ids("署名検証"), [CX])

    def test_a_codex_rollout_that_gets_compressed_stays_one_session(self):
        # The plain file is replaced by the compressed one; the session must neither vanish nor show up twice.
        plain = self.write_webhook_session()
        self.assertEqual(self.top_ids("署名検証"), [CX])
        self.write_webhook_session(compressed=True)
        plain.unlink()
        self.assertEqual(self.top_ids("署名検証"), [CX])
        self.assertEqual(S.refresh().live, 3)

    def test_archived_codex_sessions_are_searchable(self):
        # Archiving only hides a thread in Codex; the work in it is still worth finding.
        self.write_codex(CX, [codex_meta(CX), codex_user("決済Webhookの署名検証を追加して")], folder="archived_sessions")
        self.assertEqual(self.top_ids("署名検証"), [CX])

    def test_codex_imports_of_other_agents_sessions_are_skipped(self):
        # Codex can import Claude Code sessions as its own threads; indexing them would list the same work twice.
        imported = "01a1c0de-0002-7000-8000-000000000002"
        self.write_codex(imported, [
            codex_meta(imported, source="vscode"),
            codex_user("<command-name>/plan</command-name>\n<command-args>ZZIMPORTED 請求書の再発行フローを設計して</command-args>",
                       turn="external-import-turn-1"),
            codex_reply("[external_agent_tool_call: Edit]\nfile: /work/shop/billing/invoice_renderer.go", turn="external-import-turn-1"),
            codex_user("請求書の再発行フローの続き", turn="external-import-turn-2"),
        ])
        self.assertEqual(self.top_ids("ZZIMPORTED"), [])
        self.assertEqual(self.top_ids("再発行フロー"), ["aaaa1111"])

    def test_codex_subagent_threads_are_skipped(self):
        # A subagent thread is part of its parent's work, like a Claude Code sidechain, not a session the user started.
        sub = "01a1c0de-0003-7000-8000-000000000003"
        self.write_codex(sub, [codex_meta(sub, source={"subagent": "review"}), codex_user("ZZREVIEWER の観点で差分をレビューして"),
                               codex_reply("指摘はありません")])
        self.assertEqual(self.top_ids("ZZREVIEWER"), [])

    def test_legacy_codex_event_messages_are_read(self):
        # Rollouts written before Codex migrated them to item events carry the conversation as user_message and agent_message.
        old = "01a1c0de-0004-7000-8000-000000000004"
        self.write_codex(old, [
            codex_meta(old),
            codex("response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>ZZCODEXNOISE</environment_context>"}]}),
            codex("event_msg", {"type": "user_message", "message": "在庫CSVの文字化けを直して", "images": []}),
            codex("event_msg", {"type": "agent_message", "message": "Shift_JIS で読むように直しました"}),
        ])
        self.assertEqual(self.top_ids("文字化け"), [old])
        self.assertEqual(self.top_ids("Shift_JIS"), [old])
        self.assertEqual(self.top_ids("ZZCODEXNOISE"), [])

    def test_one_repo_has_one_project_name_across_agents(self):
        # -p and stats group by project; a repo worked on in both agents must not split into two names.
        self.write_webhook_session()
        self.assertEqual(self.top_ids("署名検証", project="-work-shop"), [CX])
        self.assertEqual(self.top_ids("合鍵", project="-work-shop"), ["bbbb2222"])

    def test_search_can_be_limited_to_one_agent(self):
        self.write_webhook_session()
        self.assertEqual(self.top_ids("署名検証", agent="codex"), [CX])
        self.assertEqual(self.top_ids("署名検証", agent="claude"), [])
        self.assertEqual(self.top_ids("請求書", agent="codex"), [])

    def test_cli_search_from_codex_leaves_out_the_calling_session(self):
        # Codex exposes its own thread id to the commands it runs; without this, Codex would cite its own conversation as past work.
        self.write_webhook_session()
        out = io.StringIO()
        argv = sys.argv
        sys.argv = ["agent-sessidx", "search", "署名検証"]
        os.environ["CODEX_THREAD_ID"] = CX
        try:
            with redirect_stdout(out):
                S.main()
        finally:
            sys.argv = argv
        self.assertEqual(out.getvalue().strip(), "no match")

    def test_show_prints_a_codex_session(self):
        # show is what an agent reads before relying on a past Codex session, and it must say how to resume it.
        self.write_webhook_session()
        self.name_codex(CX, "Stripe Webhookの署名検証")
        out = io.StringIO()
        with redirect_stdout(out):
            S.show(S.refresh(), CX[:13], 30)
        text = out.getvalue()
        for expected in ["agent: codex", "cwd: /work/shop", "branch: feature/webhook-signature", "title: Stripe Webhookの署名検証",
                         f"codex resume {CX}", "決済Webhookの署名検証を追加して", "リトライ時の冪等キーも確認して", "CIの結果も見て",
                         "/work/shop/payments/webhook_verifier.go", "https://github.com/example/shop/pull/5151", "CIはすべて通りました"]:
            self.assertIn(expected, text)
        for unexpected in ["ZZCODEXNOISE", "ZZOPENTAB", "改ざん対策"]:
            self.assertNotIn(unexpected, text)


if __name__ == "__main__":
    unittest.main()
