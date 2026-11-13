#!/usr/bin/env python3
import argparse
import fcntl
import json
import marshal
import math
import os
import re
import sys
import time
import unicodedata
from array import array
from collections import Counter

VERSION = 9
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
INDEX_DIR = os.path.expanduser("~/.claude/agent-sessidx")
INDEX_PATH = os.path.join(INDEX_DIR, f"index-v{VERSION}.bin")  # per version, so installs of different versions never overwrite each other's index
LOCK_PATH = os.path.join(INDEX_DIR, "lock")
EXCLUDE_PROJECT = ("observer-sessions",)

W_TITLE, W_FIRST, W_PROMPT, W_LAST_REPLY, W_FILE, W_META = 4, 2, 1, 1, 1, 2
MAX_PROMPT_CHARS = 2000
MAX_TEXT_CHARS = 40000
K1, B = 1.2, 0.5
COMPACT_RATIO = 0.25

CJK = "々〇぀-ヿ㐀-鿿豈-﫿"
TOKEN_RE = re.compile(rf"[A-Za-z0-9_]+|[{CJK}]+")
CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
HIRAGANA_RE = re.compile(r"^[぀-ゟー]+$")
SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
BARE_COMMAND_RE = re.compile(r"/[A-Za-z0-9][\w:.-]*")
WANTED_MARKERS = tuple(b'"type":"%s"' % t for t in (b"user", b"assistant", b"ai-title", b"custom-title", b"pr-link", b"queued_command"))
COPY_PREFIX = "<transcript>"
AUTO_PREFIX = "<scheduled-task"
NOISE_PREFIXES = ("<local-command", "<task-notification", "<agent-message", "<bash-stdout", "<bash-stderr", "Caveat:", "[Request interrupted")
DELEGATION_TOOLS = ("Agent", "Task")


def is_cjk(ch):
    return ch >= "　"


def ascii_parts(w):
    parts = {w.lower(), *(p.lower() for p in w.split("_")), *(p.lower() for p in CAMEL_RE.findall(w))}
    return [p for p in parts if 2 <= len(p) <= 32 or p.isdigit()]


def tokenize(text):
    text = unicodedata.normalize("NFKC", text)
    for m in TOKEN_RE.finditer(text):
        w = m.group()
        if not is_cjk(w[0]):
            yield from ascii_parts(w)
        elif len(w) == 1:
            yield w
        else:
            for i in range(len(w) - 1):
                yield w[i:i + 2]


def query_terms(query, vocabulary=None):
    """Hiragana bigrams are dropped only when they come from a run mixed with kanji or katakana (particles), so a pure-hiragana word like いいね still counts.
    An ASCII word is searched as a whole when the index knows it; its snake/camel parts are used only as a fallback, so serviceKind does not also match every "service"."""
    terms = []
    for m in TOKEN_RE.finditer(unicodedata.normalize("NFKC", query)):
        w = m.group()
        if not is_cjk(w[0]):
            whole = w.lower()
            known = vocabulary is None or whole in vocabulary
            terms += [whole] if known and (2 <= len(whole) <= 32 or whole.isdigit()) else ascii_parts(w)
        elif len(w) == 1:
            terms.append(w)
        else:
            pure = bool(HIRAGANA_RE.match(w))
            terms += [w[i:i + 2] for i in range(len(w) - 1) if pure or not HIRAGANA_RE.match(w[i:i + 2])]
    return list(dict.fromkeys(terms))


def clean_prompt(text):
    text = SYSTEM_REMINDER_RE.sub("", text).strip()
    if not text or text.startswith(NOISE_PREFIXES):
        return ""
    name = COMMAND_NAME_RE.search(text)
    if name:
        args = COMMAND_ARGS_RE.search(text)
        text = f"{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    return text[:MAX_PROMPT_CHARS]


def worth_parsing(line):
    if not any(m in line for m in WANTED_MARKERS):
        return False
    is_tool_output = b'"tool_result"' in line and b'"type":"assistant"' not in line
    return not is_tool_output or b'"type":"text"' in line


def new_session(path, agent, session_id, project):
    """not_own: the file is not a session of its own (a summarizer copy, an import, a subagent thread) and is kept out of the index.
    auto: a scheduled-task session, left out of results unless asked for."""
    return {"id": session_id, "agent": agent, "path": path, "project": project, "cwd": None, "branch": None,
            "entrypoint": None, "start": None, "end": None, "title": None, "prompts": [], "delegations": [],
            "files": Counter(), "prs": [], "last_reply": "", "bad_lines": 0, "not_own": False, "auto": False}


def parse_claude(path, stop_at_copy=False):
    """stop_at_copy stops reading once the first prompt shows the file is a summarizer copy; the index needs nothing else from it."""
    prompts, delegations, files, prs = [], [], Counter(), []
    opening = ""  # first prompt before command substitution, so copies and scheduled tasks are recognised even if they quote a slash command
    title = custom_title = cwd = branch = entrypoint = None
    start = end = None
    last_reply = ""
    bad_lines = 0
    with open(path, "rb") as f:
        for line in f:
            if not worth_parsing(line):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                bad_lines += 1
                continue
            kind = d.get("type")
            ts = d.get("timestamp")
            if ts:
                start = start or ts
                end = ts
            if kind == "ai-title":
                title = d.get("aiTitle") or title
            elif kind == "custom-title":
                custom_title = d.get("customTitle") or custom_title
            elif kind == "pr-link":
                if d.get("prUrl") and d["prUrl"] not in prs:
                    prs.append(d["prUrl"])
            elif kind == "attachment":
                att = d.get("attachment") or {}
                if att.get("type") == "queued_command" and isinstance(att.get("prompt"), str) and not d.get("isSidechain"):
                    p = clean_prompt(att["prompt"])
                    if p:
                        opening = opening if prompts else SYSTEM_REMINDER_RE.sub("", att["prompt"]).strip()
                        prompts.append(p)
            elif kind == "user":
                if d.get("isMeta") or d.get("isSidechain") or d.get("isCompactSummary"):
                    continue
                cwd = cwd or d.get("cwd")
                branch = d.get("gitBranch") or branch
                entrypoint = entrypoint or d.get("entrypoint")
                content = d.get("message", {}).get("content")
                parts = [content] if isinstance(content, str) else [
                    p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text"]
                for raw in parts:
                    p = clean_prompt(raw)
                    if p:
                        opening = opening if prompts else SYSTEM_REMINDER_RE.sub("", raw).strip()
                        prompts.append(p)
            elif kind == "assistant":
                texts = []
                for p in d.get("message", {}).get("content") or []:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        texts.append(p.get("text", ""))
                    elif p.get("type") == "tool_use":
                        inp = p.get("input") or {}
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if isinstance(fp, str):
                            files[fp] += 1
                        if p.get("name") in DELEGATION_TOOLS and isinstance(inp.get("prompt"), str):
                            delegations.append(inp["prompt"][:MAX_PROMPT_CHARS])
                if texts:
                    last_reply = "\n".join(texts)
            if stop_at_copy and prompts:
                if opening.startswith(COPY_PREFIX):
                    break
                stop_at_copy = False
    s = new_session(path, "claude", os.path.basename(path)[:-6], os.path.basename(os.path.dirname(path)))
    s.update(cwd=cwd, branch=branch, entrypoint=entrypoint, start=start, end=end, title=custom_title or title,
             prompts=prompts, delegations=delegations, files=files, prs=prs, last_reply=last_reply, bad_lines=bad_lines,
             not_own=opening.startswith(COPY_PREFIX), auto=opening.startswith(AUTO_PREFIX))
    return s


def index_parse_claude(path):
    return parse_claude(path, stop_at_copy=True)


def list_claude():
    if not os.path.isdir(CLAUDE_PROJECTS):
        return
    with os.scandir(CLAUDE_PROJECTS) as projects:
        for proj in projects:
            if not proj.is_dir() or any(x in proj.name for x in EXCLUDE_PROJECT):
                continue
            with os.scandir(proj.path) as entries:
                for e in entries:
                    if e.name.endswith(".jsonl") and e.is_file():
                        yield e.path


def codex_dirs():
    return [os.path.join(CODEX_HOME, d) for d in ("sessions", "archived_sessions")]


def walk_codex():
    for top in codex_dirs():
        for root, _, names in os.walk(top):
            for n in names:
                if n.startswith("rollout-") and (n.endswith(".jsonl") or n.endswith(".jsonl.zst")):
                    yield os.path.join(root, n)


_zstd_state = []


def zstd_reader():
    """'module' when compression.zstd (Python 3.14+) is there, else the path of the zstd command, else None."""
    if not _zstd_state:
        try:
            import compression.zstd  # noqa: F401
            _zstd_state.append("module")
        except ImportError:
            import shutil
            _zstd_state.append(shutil.which("zstd"))
    return _zstd_state[0]


def list_codex():
    can_unzstd = None
    for path in walk_codex():
        if path.endswith(".zst"):
            if can_unzstd is None:
                can_unzstd = bool(zstd_reader())
            if not can_unzstd:
                continue
        yield path


def read_lines(path):
    """Yields the raw lines of a rollout, decompressing .zst as a stream. Raises OSError if decompression fails midway."""
    if not path.endswith(".zst"):
        with open(path, "rb") as f:
            yield from f
        return
    how = zstd_reader()
    if how == "module":
        from compression import zstd
        try:
            with zstd.open(path, "rb") as f:
                yield from f
        except (zstd.ZstdError, EOFError) as e:  # a corrupt or truncated stream, reported the same way as the zstd command's failure
            raise OSError(f"zstd: {e}") from e
        return
    if not how:
        raise OSError("zstd is not available")
    import subprocess
    with open(path, "rb") as src:  # opened here so a vanished file raises FileNotFoundError like a plain rollout
        proc = subprocess.Popen([how, "-dcq"], stdin=src, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    finished = False
    try:
        yield from proc.stdout
        finished = True
    finally:
        if not finished and proc.poll() is None:  # after EOF zstd may still be exiting; killing it then would turn success into -9
            proc.kill()
        proc.stdout.close()
        code = proc.wait()
    if finished and code != 0:
        raise OSError(f"zstd exited with {code}")


_codex_titles = {}


def codex_titles():
    """Thread names from session_index.jsonl, the last line for an id winning; re-read whenever the file changes."""
    path = os.path.join(CODEX_HOME, "session_index.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (path, st.st_mtime_ns, st.st_size)
    if key not in _codex_titles:
        names = {}
        with open(path, "rb") as f:
            for raw in f:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(d, dict) and isinstance(d.get("id"), str) and d.get("thread_name"):
                    names[d["id"]] = d["thread_name"]
        _codex_titles.clear()
        _codex_titles[key] = names
    return _codex_titles[key]


CODEX_MARKERS = (b'"session_meta"', b'"UserMessage"', b'"AgentMessage"', b'"FileChange"', b'"user_message"', b'"agent_message"')
CODEX_REQUEST_RE = re.compile(r"^## My request(?: for Codex)?:[ \t]*$", re.M)
PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IMPORT_TURN_PREFIX = "external-import-turn-"


def codex_request(text):
    """Codex Desktop and the IDE extensions put context blocks before the request; keep only what follows the last request heading."""
    marks = list(CODEX_REQUEST_RE.finditer(text))
    if marks:
        tail = text[marks[-1].end():].strip()
        if tail:
            text = tail
    return text.strip()[:MAX_PROMPT_CHARS]


def codex_text(content, kind=None):
    return "\n".join(c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)
                     and (kind is None or c.get("type") == kind)) if isinstance(content, list) else ""


def id_from_name(path):
    """The first UUID in the file name: Claude's <id>.jsonl, and Codex's rollout-<time>-<id>[_<page>].jsonl[.zst]."""
    name = os.path.basename(path)
    m = UUID_RE.search(name)
    return m.group() if m else name.split(".", 1)[0]


def project_of_cwd(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd) if cwd else ""


def parse_codex(path, for_index=False):
    """for_index stops reading once the rollout is known to be a subagent thread or an import; the index needs nothing else from it."""
    sid = cwd = branch = entrypoint = start = end = None
    prompts, replies, files = [], [], Counter()
    old_prompts, old_replies = [], []  # rollouts not yet migrated to item events; used only when a file has no item events
    bad_lines = 0
    not_own = False
    seen_user = False
    lines = read_lines(path)
    try:
        for raw in lines:
            if not any(m in raw for m in CODEX_MARKERS):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                bad_lines += 1
                continue
            if not isinstance(d, dict):
                continue
            kind, p = d.get("type"), d.get("payload")
            if not isinstance(p, dict):
                continue
            ts = d.get("timestamp")
            if ts:
                start = start or ts
                end = ts
            if kind == "session_meta":
                sid = sid or p.get("id")
                cwd = cwd or p.get("cwd")
                git = p.get("git")
                branch = branch or (git.get("branch") if isinstance(git, dict) else None)
                src = p.get("source")
                if isinstance(src, str):
                    entrypoint = entrypoint or src
                elif isinstance(src, dict) and "subagent" in src:
                    not_own = True
                    if for_index:
                        break
                continue
            if kind != "event_msg":
                continue
            ptype = p.get("type")
            if ptype == "user_message" and isinstance(p.get("message"), str):
                text = codex_request(p["message"])
                if text:
                    old_prompts.append(text)
            elif ptype == "agent_message" and isinstance(p.get("message"), str):
                old_replies.append(p["message"])
            elif ptype == "item_completed" and isinstance(p.get("item"), dict):
                item = p["item"]
                itype = item.get("type")
                if itype == "UserMessage":
                    if not seen_user and str(p.get("turn_id") or "").startswith(IMPORT_TURN_PREFIX):
                        not_own = True
                        if for_index:
                            break
                    seen_user = True
                    text = codex_request(codex_text(item.get("content"), "text"))
                    if text:
                        prompts.append(text)
                elif itype == "AgentMessage":
                    replies.append(codex_text(item.get("content")))
                elif itype == "FileChange" and isinstance(item.get("changes"), dict):
                    for fp in item["changes"]:
                        files[fp] += 1
    except FileNotFoundError:
        raise
    except OSError:
        bad_lines += 1  # a broken compressed stream; keep what was read
    finally:
        lines.close()  # stops and reaps the zstd process when reading ends early
    if not prompts and not replies:
        prompts, replies = old_prompts, old_replies
    sid = sid or id_from_name(path)
    prs = []
    for r in replies:
        for url in PR_URL_RE.findall(r):
            if url not in prs:
                prs.append(url)
    s = new_session(path, "codex", sid, project_of_cwd(cwd))
    s.update(cwd=cwd, branch=branch, entrypoint=entrypoint, start=start, end=end, title=codex_titles().get(sid),
             prompts=prompts, files=files, prs=prs, last_reply=replies[-1] if replies else "", bad_lines=bad_lines, not_own=not_own)
    return s


def index_parse_codex(path):
    return parse_codex(path, for_index=True)


def codex_rollouts_left_out():
    """Number of .zst rollouts that cannot be read on this machine."""
    if zstd_reader():
        return 0
    return sum(1 for p in walk_codex() if p.endswith(".zst"))


SOURCES = {
    # index_parse (optional) is a cheaper parse used only for indexing; it may stop early on files that are not sessions of their own.
    "claude": {"list": list_claude, "parse": parse_claude, "index_parse": index_parse_claude,
               "self_env": "CLAUDE_CODE_SESSION_ID", "resume": "claude --resume {id}"},
    "codex": {"list": list_codex, "parse": parse_codex, "index_parse": index_parse_codex,
              "self_env": "CODEX_THREAD_ID", "resume": "codex resume {id}"},
}


def parse_session(path, agent, for_index=False):
    source = SOURCES[agent]
    parse = (for_index and source.get("index_parse")) or source["parse"]
    return parse(path)


def first_request_at(prompts):
    """A bare slash command such as /model says nothing about the work, so the first request is the first prompt that is not one; a session of only commands keeps its first."""
    for i, p in enumerate(prompts):
        if not BARE_COMMAND_RE.fullmatch(p):
            return i
    return 0


def summarize(s):
    prompts = s["prompts"]
    return {
        "id": s["id"], "agent": s["agent"], "path": s["path"], "project": s["project"], "cwd": s["cwd"], "branch": s["branch"],
        "entrypoint": s["entrypoint"], "start": s["start"], "end": s["end"], "title": s["title"],
        "first": prompts[first_request_at(prompts)][:300] if prompts else "",
        "last": s["prompts"][-1][:300] if len(s["prompts"]) > 1 else "",
        "n_prompts": len(s["prompts"]), "prs": s["prs"], "bad_lines": s["bad_lines"],
        "auto": bool(s.get("auto")),
    }


def weighted_terms(s):
    tf = Counter()

    def add(text, w):
        for tok in tokenize(text):
            tf[tok] += w

    add(s["title"] or "", W_TITLE)
    prompts = s["prompts"]
    first = first_request_at(prompts)
    if prompts:
        add(prompts[first], W_FIRST)
    budget = MAX_TEXT_CHARS
    for p in prompts[:first] + prompts[first + 1:] + s["delegations"]:
        if budget <= 0:
            break
        add(p[:budget], W_PROMPT)
        budget -= len(p)
    add(s["last_reply"][:MAX_PROMPT_CHARS], W_LAST_REPLY)
    for fp in s["files"]:
        add(fp, W_FILE)
    add(" ".join([s["branch"] or "", s["project"], *s["prs"]]), W_META)
    return tf


def index_one(path, agent):
    try:
        s = parse_session(path, agent, for_index=True)
    except FileNotFoundError:
        return path, None, None  # not recorded at all, so a file that later appears at this path is read on the next refresh
    if s.get("not_own"):
        return path, None, {}
    return path, summarize(s), dict(weighted_terms(s))


class Index:
    """Append-only inverted index; changed sessions are tombstoned and re-appended, so doc ids stay sorted in every posting list."""

    def __init__(self):
        self.files = {}
        self.docs = []
        self.doc_len = array("f")
        self.postings = {}
        self.live = 0
        self.total_len = 0.0

    def posting(self, term):
        p = self.postings.get(term)
        if p is not None and isinstance(p[0], bytes):
            ids, tfs = array("I"), array("H")
            ids.frombytes(p[0])
            tfs.frombytes(p[1])
            p = self.postings[term] = (ids, tfs)
        return p

    def live_postings(self, term):
        """A single kanji is stored only inside bigrams, so a one-character query term is answered from every bigram that contains it."""
        merged = Counter()
        sources = [term]
        if len(term) == 1 and is_cjk(term):
            sources += [t for t in self.postings if len(t) == 2 and term in t]
        for t in sources:
            p = self.posting(t)
            if p is None:
                continue
            for d, tf in zip(*p):
                if self.docs[d] is not None:
                    merged[d] += tf
        return merged

    def add(self, path, stat_key, meta, tf):
        if meta is None:
            self.files[path] = [stat_key[0], stat_key[1], -1]
            return
        doc_id = len(self.docs)
        self.docs.append(meta)
        length = float(sum(tf.values()))
        self.doc_len.append(length)
        self.total_len += length
        self.live += 1
        for term, n in tf.items():
            p = self.posting(term)
            if p is None:
                p = self.postings[term] = (array("I"), array("H"))
            p[0].append(doc_id)
            p[1].append(min(n, 65535))
        self.files[path] = [stat_key[0], stat_key[1], doc_id]

    def remove(self, path):
        entry = self.files.pop(path, None)
        if entry is None or entry[2] < 0 or self.docs[entry[2]] is None:
            return
        doc_id = entry[2]
        self.docs[doc_id] = None
        self.total_len -= self.doc_len[doc_id]
        self.live -= 1

    def compact(self):
        remap = {}
        new = Index()
        for old_id, meta in enumerate(self.docs):
            if meta is not None:
                remap[old_id] = len(new.docs)
                new.docs.append(meta)
                new.doc_len.append(self.doc_len[old_id])
        for term in list(self.postings):
            ids, tfs = self.posting(term)
            nids, ntfs = array("I"), array("H")
            for i, n in zip(ids, tfs):
                j = remap.get(i)
                if j is not None:
                    nids.append(j)
                    ntfs.append(n)
            if nids:
                new.postings[term] = (nids, ntfs)
        new.files = {p: [m, s, remap.get(d, -1)] for p, (m, s, d) in self.files.items() if d < 0 or d in remap}
        new.live = len(new.docs)
        new.total_len = float(sum(new.doc_len))
        return new

    def dump(self):
        postings = {t: p if isinstance(p[0], bytes) else (p[0].tobytes(), p[1].tobytes()) for t, p in self.postings.items()}
        return marshal.dumps({"version": VERSION, "files": self.files, "docs": self.docs,
                              "doc_len": self.doc_len.tobytes(), "postings": postings,
                              "live": self.live, "total_len": self.total_len})

    @classmethod
    def load(cls, blob):
        d = marshal.loads(blob)
        if not isinstance(d, dict) or d.get("version") != VERSION:
            raise ValueError("index version mismatch")
        idx = cls()
        idx.files, idx.docs, idx.postings = d["files"], d["docs"], d["postings"]
        idx.doc_len.frombytes(d["doc_len"])
        idx.live, idx.total_len = d["live"], d["total_len"]
        if len(idx.doc_len) != len(idx.docs) or idx.live != sum(m is not None for m in idx.docs):
            raise ValueError("index is inconsistent")
        return idx


def list_sessions(known=None):
    """known is the index's file table. A transcript recorded there as a summarizer copy (doc id -1) stays a copy however it grows,
    since that is decided by its first request alone, so it is not stat'ed; its recorded mtime and size are reused while it is still listed."""
    known = known or {}
    out = {}
    for agent, source in SOURCES.items():
        for path in source["list"]():
            entry = known.get(path)
            if entry is not None and entry[2] < 0:
                out[path] = (entry[0], entry[1], agent)
                continue
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            out[path] = (st.st_mtime_ns, st.st_size, agent)
    return out


def load_index():
    try:
        with open(INDEX_PATH, "rb") as f:
            return Index.load(f.read())
    except Exception:
        return Index()


def save_index(idx):
    tmp = f"{INDEX_PATH}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(idx.dump())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, INDEX_PATH)


def session_id_of(idx, path):
    entry = idx.files.get(path)
    if entry is not None and entry[2] >= 0 and idx.docs[entry[2]] is not None:
        return idx.docs[entry[2]]["id"]
    return id_from_name(path)


def open_lock():
    """The locked lock file, or None where the index dir cannot be written, as inside Codex's default sandbox;
    the index is then refreshed in memory only."""
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        lock = open(LOCK_PATH, "w")
    except OSError:
        return None
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def refresh(force=False, skip_ids=()):
    """skip_ids names sessions whose changes are left for a later call (a search that excludes the caller's own session
    gets the same results from a stale entry); they are neither re-read nor added now."""
    lock = open_lock()
    try:
        idx = Index() if force else load_index()
        current = list_sessions(idx.files)
        changed = [p for p, key in current.items() if tuple(idx.files.get(p, (None, None))[:2]) != key[:2]]
        if skip_ids and not force:
            changed = [p for p in changed if session_id_of(idx, p) not in skip_ids]
        gone = [p for p in idx.files if p not in current]
        if not changed and not gone:
            return idx
        for p in gone + changed:
            idx.remove(p)
        started = time.time()
        if len(changed) > 8:
            from concurrent.futures import ProcessPoolExecutor  # ~30ms to import, so only when a pool is actually used
            with ProcessPoolExecutor() as pool:
                results = list(pool.map(index_one, changed, [current[p][2] for p in changed], chunksize=8))
        else:
            results = [index_one(p, current[p][2]) for p in changed]
        for path, meta, tf in results:
            if tf is not None:
                idx.add(path, current[path][:2], meta, tf)
        if idx.docs and (len(idx.docs) - idx.live) / len(idx.docs) > COMPACT_RATIO:
            idx = idx.compact()
        if lock:
            save_index(idx)
        else:
            print(f"note: {INDEX_DIR} is not writable here, so the index was refreshed in memory without saving it "
                  f"({len(changed)} transcripts re-read)", file=sys.stderr)
        if len(changed) > 8:
            print(f"indexed {len(changed)} sessions in {time.time() - started:.1f}s", file=sys.stderr)
        empty = sum(1 for _, meta, _ in results if meta is not None and meta["n_prompts"] == 0)
        if len(results) >= 10 and empty / len(results) > 0.5:
            print(f"warning: {empty}/{len(results)} re-indexed sessions have no prompts; the transcript format may have changed", file=sys.stderr)
        return idx
    finally:
        if lock:
            lock.close()


def epoch(ts):
    """UTC seconds of ts[:19] (YYYY-MM-DDTHH:MM:SS), computed directly; calendar and _strptime cost ~14ms to import."""
    y, mo, d = int(ts[0:4]), int(ts[5:7]), int(ts[8:10])
    h, mi, s = int(ts[11:13]), int(ts[14:16]), int(ts[17:19])
    if ts[4] != "-" or ts[7] != "-" or ts[10] != "T" or ts[13] != ":" or ts[16] != ":" or not 1 <= mo <= 12:
        raise ValueError(f"bad timestamp: {ts!r}")
    y -= mo <= 2  # count years from March so the leap day falls at the end
    era, yoe = divmod(y, 400)
    doy = (153 * (mo - 3 if mo > 2 else mo + 9) + 2) // 5 + d - 1
    days = era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468
    return days * 86400 + h * 3600 + mi * 60 + s


def age_days(ts):
    return max(0.0, (time.time() - epoch(ts)) / 86400) if ts else 365.0


def search(idx, query, limit, project=None, since_days=None, include_auto=False, exclude_ids=(), agent=None):
    terms = query_terms(query, idx.postings)
    if not terms or not idx.live:
        return []
    n = idx.live
    avgdl = idx.total_len / idx.live
    scores, hits = {}, Counter()
    for term in terms:
        postings = idx.live_postings(term)
        if not postings:
            continue
        idf = math.log(1 + (n - len(postings) + 0.5) / (len(postings) + 0.5))
        for d, tf in postings.items():
            norm = K1 * (1 - B + B * idx.doc_len[d] / avgdl)
            scores[d] = scores.get(d, 0.0) + idf * tf * (K1 + 1) / (tf + norm)
            hits[d] += 1
    best = {}
    for d, sc in scores.items():
        meta = idx.docs[d]
        if meta["id"] in exclude_ids or (meta["auto"] and not include_auto) or (agent and meta["agent"] != agent):
            continue
        if project and project not in meta["project"] and project not in (meta["cwd"] or ""):
            continue
        age = age_days(meta["end"])
        if since_days is not None and age > since_days:
            continue
        coverage = hits[d] / len(terms)
        engagement = min(1.0, math.log2(1 + meta["n_prompts"]) / 4)
        score = sc * (0.4 + 0.6 * coverage) * (0.6 + 0.4 * engagement) * (1 + 0.3 * math.exp(-age / 30))
        if score > best.get(meta["id"], (0, None))[0]:
            best[meta["id"]] = (score, meta)
    return sorted(best.values(), key=lambda x: -x[0])[:limit]


def short(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def local_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch(ts))) if ts else "?"


def print_hits(ranked):
    if not ranked:
        print("no match")
        return
    for i, (score, m) in enumerate(ranked, 1):
        print(f"{i}. {m['id']}  [{m['agent']}]  {local_time(m['end'])}  {m['project']}  branch={m['branch'] or '-'}  score={score:.1f}")
        if m["title"]:
            print(f"   title: {short(m['title'], 100)}")
        print(f"   first: {short(m['first'], 160)}")
        if m["last"]:
            print(f"   last:  {short(m['last'], 160)}")
        if m["prs"]:
            print(f"   prs:   {' '.join(m['prs'][-3:])}")


def find_session(idx, prefix):
    matches = [m for m in idx.docs if m is not None and m["id"].startswith(prefix)]
    ids = {m["id"] for m in matches}
    if len(ids) != 1:
        sys.exit(f"{len(ids)} sessions match {prefix!r}")
    if len(matches) > 1:
        print(f"note: {len(matches)} copies of this session; showing the most recently active one", file=sys.stderr)
    return max(matches, key=lambda m: m["end"] or "")


def show(idx, prefix, max_prompts):
    meta = find_session(idx, prefix)
    s = parse_session(meta["path"], meta["agent"])
    print(f"id: {s['id']}\nagent: {s['agent']}\nproject: {s['project']}\ncwd: {s['cwd']}\nbranch: {s['branch']}")
    print(f"time: {local_time(s['start'])} → {local_time(s['end'])}\ntitle: {s['title'] or '-'}")
    resume = SOURCES[s["agent"]].get("resume")
    if resume:
        print(f"resume: cd {s['cwd']} && {resume.format(id=s['id'])}")
    if s["prs"]:
        print("prs: " + " ".join(s["prs"]))
    prompts = s["prompts"]
    print(f"\n## prompts ({len(prompts)})")
    if len(prompts) > max_prompts:
        half = max_prompts // 2
        prompts = prompts[:half] + ["…"] + prompts[-(max_prompts - half):]
    for p in prompts:
        print(f"- {short(p, 300)}")
    if s["delegations"]:
        print(f"\n## delegated to subagents ({len(s['delegations'])})")
        for p in s["delegations"][:10]:
            print(f"- {short(p, 200)}")
    if s["files"]:
        print("\n## files touched")
        for fp, n in s["files"].most_common(20):
            print(f"- {fp} ({n})")
    print("\n## last reply")
    print(s["last_reply"][:2000])


def stats(idx):
    size = os.path.getsize(INDEX_PATH) if os.path.exists(INDEX_PATH) else 0
    live = [m for m in idx.docs if m is not None]
    copies = sum(1 for _, _, d in idx.files.values() if d < 0)
    no_prompts = sum(1 for m in live if m["n_prompts"] == 0)
    bad = sum(m["bad_lines"] for m in live)
    print(f"sessions: {idx.live}  tombstones: {len(idx.docs) - idx.live}  terms: {len(idx.postings)}  index: {size / 1048576:.1f}MB")
    print(f"skipped (copies, imports, subagent threads): {copies}  sessions without prompts: {no_prompts}  unparsable lines: {bad}")
    print("agents: " + "  ".join(f"{a}={n}" for a, n in Counter(m["agent"] for m in live).most_common()))
    for proj, n in Counter(m["project"] for m in live).most_common(10):
        print(f"  {n:5d} {proj}")
    missing = codex_rollouts_left_out()
    if missing:
        print(f"codex: {missing} .zst rollouts are not indexed because zstd is unavailable (install zstd or run with Python 3.14+)")


def calling_session_ids():
    names = [src.get("self_env") for src in SOURCES.values()]
    return {os.environ[n] for n in names if n and os.environ.get(n)}


def main():
    ap = argparse.ArgumentParser(prog="agent-sessidx", description="Search past coding-agent sessions (Claude Code and Codex) by what was done in them.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query", nargs="+")
    s.add_argument("-n", type=int, default=8)
    s.add_argument("-p", "--project", help="substring of the project dir name or cwd")
    s.add_argument("--days", type=float, help="only sessions active within N days")
    s.add_argument("--auto", action="store_true", help="include scheduled-task sessions")
    s.add_argument("--agent", choices=sorted(SOURCES), help="only sessions of this agent")
    s.add_argument("--include-self", action="store_true", help="keep the calling session in results")
    sh = sub.add_parser("show")
    sh.add_argument("session_id")
    sh.add_argument("--prompts", type=int, default=30)
    sub.add_parser("rebuild")
    sub.add_parser("stats")
    a = ap.parse_args()

    logged = {"search": " ".join(getattr(a, "query", [])), "show": getattr(a, "session_id", "")}
    top, ok = [], False
    try:
        exclude = calling_session_ids() if a.cmd == "search" and not a.include_self else ()
        # the caller's own transcript grows with every search; it is excluded from the results, so re-reading it can wait
        idx = refresh(force=a.cmd == "rebuild", skip_ids=exclude)
        if a.cmd == "search":
            ranked = search(idx, " ".join(a.query), a.n, a.project, a.days, a.auto, exclude, a.agent)
            print_hits(ranked)
            top = [m["id"] for _, m in ranked]
        elif a.cmd == "show":
            top = [find_session(idx, a.session_id)["id"]]
            show(idx, a.session_id, a.prompts)
        else:
            stats(idx)
        ok = True
    finally:
        if a.cmd in logged:
            log_usage(a.cmd, logged[a.cmd], top, ok)


def log_usage(cmd, arg, ids, ok):
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cmd": cmd, "arg": arg, "ok": ok, "ids": ids[:3],
             "caller": sorted(calling_session_ids()), "cwd": cwd}
    if cmd == "search":
        entry["hits"] = len(ids)
    try:
        os.makedirs(INDEX_DIR, exist_ok=True)
        with open(os.path.join(INDEX_DIR, "usage.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"note: usage log not written: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
