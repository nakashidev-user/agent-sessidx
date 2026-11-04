import importlib.util
import io
import json
import os
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


class SessionIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.projects = base / "projects"
        (self.projects / "-work-shop").mkdir(parents=True)
        self.saved = (S.CLAUDE_PROJECTS, S.INDEX_DIR, S.INDEX_PATH, S.LOCK_PATH)
        S.CLAUDE_PROJECTS = str(self.projects)
        S.INDEX_DIR = str(base / "index")
        S.INDEX_PATH = os.path.join(S.INDEX_DIR, "index.bin")
        S.LOCK_PATH = os.path.join(S.INDEX_DIR, "lock")

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
        S.CLAUDE_PROJECTS, S.INDEX_DIR, S.INDEX_PATH, S.LOCK_PATH = self.saved
        self.tmp.cleanup()

    def write(self, session_id, lines, project="-work-shop", mode="w"):
        path = self.projects / project / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode, encoding="utf-8") as f:
            f.writelines(lines)
        return path

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


if __name__ == "__main__":
    unittest.main()
