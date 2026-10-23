"""
runner.py
----------
Bridges the AutoCNIC library (vendored in ./autocnic/) to the web app: clones
a GitHub repo, hands it to a worker process (worker.py) that loads its
AutoCNIC-compatible pipeline definition, runs discovery (perturbation
experiments + predicate mining) to build a CNIC dependency graph, and drives
live updates through the real Algorithm 1/2 engine. This module persists the
certificates and history the worker returns.

Repo convention (see demo_repos/ for working examples): a repo usable here
must contain a module `autocnic_pipeline.py` defining:

    PIPELINE: autocnic.pipeline.Pipeline   -- built with .input()/.step(...)
    SAMPLERS: Dict[str, Callable[[], Any]] -- one random-value sampler per
                                               input node, used for perturbation
                                               experiments during discovery
    INITIAL_INPUTS: Dict[str, Any]         -- optional; starting input values
                                               shown when the graph first loads
                                               (falls back to one sampler draw
                                               per input if omitted)

The repo just needs `from autocnic.pipeline import Pipeline`, which resolves
against this backend's own vendored copy (the cloned repo does not need to
ship or install AutoCNIC itself).

Isolation: a project's code only ever runs in its session's worker process,
with a scrubbed environment, resource limits (Linux) and a wall-clock timeout
per request. A worker that hangs, crashes or exits is killed and its session
drops to read-only; the server keeps serving everyone else.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import settings

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "demo_repos")
WORKER_PATH = os.path.join(BACKEND_DIR, "worker.py")
CLONE_ROOT = os.path.join(settings.DATA_DIR, "_clones")
CERT_STORE_DIR = os.path.join(settings.DATA_DIR, "cert_store")

PIPELINE_FILE = "autocnic_pipeline.py"
_SESSION_ID = re.compile(r"^[0-9a-f]{12}$")


class RepoLoadError(RuntimeError):
    pass


def _rmtree(path: str):
    """rmtree that also removes read-only files (git objects on Windows)."""
    def make_writable_and_retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=make_writable_and_retry)
        else:
            shutil.rmtree(path, onerror=make_writable_and_retry)
    except OSError:
        pass


# No session is live after a restart, so clones left over from the last run
# are dead weight on the disk.
_rmtree(CLONE_ROOT)
os.makedirs(CLONE_ROOT, exist_ok=True)
os.makedirs(CERT_STORE_DIR, exist_ok=True)


# ---- worker processes -----------------------------------------------------

class Worker:
    """One child process running worker.py for one session."""

    def __init__(self, session_id: str, cwd: str):
        self.log_path = os.path.join(CLONE_ROOT, f"{session_id}.log")
        self._log = open(self.log_path, "ab")
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL")
               if k in os.environ}
        env["HOME"] = cwd
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [sys.executable, "-I", "-u", WORKER_PATH,
               str(settings.WORKER_MEMORY_MB), str(settings.MAX_REPO_MB)]
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, text=True, encoding="utf-8",
            start_new_session=(os.name == "posix"),
        )
        self._replies: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self._replies.put(line)
        self._replies.put(None)

    def request(self, payload: dict, timeout: float) -> dict:
        """Send one request and wait for its reply. Any failure kills the worker
        and raises RepoLoadError with a message fit to show the user."""
        try:
            self.proc.stdin.write(json.dumps(payload, default=str) + "\n")
            self.proc.stdin.flush()
            line = self._replies.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise RepoLoadError(
                f"the project's code did not finish within {timeout:.0f}s (an infinite loop, or "
                f"too much work per step?). It was stopped.")
        except (BrokenPipeError, OSError):
            line = None
        if line is None:
            self.close()
            raise RepoLoadError(f"the project's code stopped the process ({self._exit_reason()}).{self._log_tail()}")
        reply = json.loads(line)
        if reply["ok"]:
            return reply["result"]
        if reply.get("user_error"):
            raise RepoLoadError(reply["error"])
        raise RuntimeError(reply["error"])

    def _exit_reason(self) -> str:
        try:
            code = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return "no response"
        if os.name == "posix" and code < 0 and -code in (signal.SIGKILL, signal.SIGSEGV):
            return f"killed by signal {-code}, possibly the {settings.WORKER_MEMORY_MB} MB memory limit"
        return f"exit code {code}"

    def _log_tail(self) -> str:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(max(0, os.path.getsize(self.log_path) - 600))
                tail = f.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""
        return f" Last output: {tail}" if tail else ""

    def close(self):
        if self.proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(self.proc.pid, signal.SIGKILL)  # also any children it forked
                else:
                    self.proc.kill()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self._log.close()


# ---- sessions -------------------------------------------------------------

@dataclass
class Session:
    id: str
    repo_url: str
    worker: Worker
    graph: dict
    state: dict
    input_nodes: List[str]
    derived_count: int
    synthesized_desc: List[dict]
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cumulative_baseline_invocations: int = 0
    cumulative_cnic_invocations: int = 0
    updates_processed: int = 0
    certificates_issued: int = 0
    certificates_verified_ok: int = 0


SESSIONS: Dict[str, Session] = {}
SESSIONS_LOCK = threading.Lock()
ANALYZE_SLOTS = threading.BoundedSemaphore(settings.MAX_CONCURRENT_ANALYSES)


GUIDE_HINT = "See the Guide page (/guide) for the expected repo layout and a starter template."


@dataclass
class Source:
    """Where a project comes from, parsed from what the user typed."""
    kind: str                  # "remote" (git clone) or "local" (folder copy)
    location: str              # clone URL or folder path
    ref: Optional[str] = None  # branch/tag from a GitHub /tree/<ref>/ URL
    subdir: str = ""           # folder inside the repo holding autocnic_pipeline.py


_GITHUB_WEB = re.compile(
    r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
    r"(?:/(?:tree|blob)/([^/]+)(?:/(.*))?)?/?$"
)
_SHORTHAND = re.compile(r"^([\w.-]+)/([\w.-]+)$")
_REMOTE = re.compile(r"^https?://[^\s]+$")


def resolve_source(text: str) -> Source:
    """Accepts a GitHub URL (including /tree/<branch>/<folder> links copied from
    the browser), `owner/repo` shorthand, any https git URL, `demo:<name>`
    for a bundled project, or -- when enabled -- a local folder / file:// URL."""
    text = text.strip()
    if text.startswith("demo:"):
        name = text[len("demo:"):]
        path = os.path.join(DEMO_DIR, name)
        if "/" in name or "\\" in name or not os.path.isdir(path):
            raise RepoLoadError(f"unknown bundled project: {name!r}")
        return Source("local", path)

    if not settings.REPOS_ENABLED:
        raise RepoLoadError(
            "this server only runs the bundled example projects (buttons below). Analyzing a "
            "repository runs its code, so the operator must set a password "
            "(AUTOCNIC_BASIC_AUTH) before other repositories are accepted."
        )

    is_file_url = text.lower().startswith("file://")
    if is_file_url or os.path.isabs(text) or os.path.isdir(text):
        if not settings.ALLOW_LOCAL_PATHS:
            raise RepoLoadError(
                "local folders and file:// URLs are disabled on this server -- push the "
                "project to GitHub and paste its URL instead."
            )
        if is_file_url:
            return Source("remote", text)
        if not os.path.isdir(text):
            raise RepoLoadError(f"folder not found: {text}")
        return Source("local", text)

    m = _GITHUB_WEB.match(text)
    if m:
        owner, repo, ref, sub = m.groups()
        return Source("remote", f"https://github.com/{owner}/{repo}.git", ref, (sub or "").strip("/"))
    m = _SHORTHAND.match(text)
    if m:
        return Source("remote", f"https://github.com/{m.group(1)}/{m.group(2)}.git")
    if text.startswith("git@"):
        raise RepoLoadError("SSH clone URLs aren't supported -- paste the https:// URL of the repository.")
    if _REMOTE.match(text):
        host = (urlparse(text).hostname or "").lower()
        if not settings.ALLOW_LOCAL_PATHS and "*" not in settings.GIT_HOSTS and host not in settings.GIT_HOSTS:
            raise RepoLoadError(
                f"this server only clones from {', '.join(settings.GIT_HOSTS)}; {host!r} is not allowed.")
        return Source("remote", text)
    raise RepoLoadError(
        "could not read that as a repository. Paste a GitHub URL "
        "(https://github.com/owner/repo), owner/repo, or a git clone URL."
    )


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _clone_repo(source: Source, session_id: str) -> str:
    dest = os.path.join(CLONE_ROOT, session_id)
    if os.path.exists(dest):
        raise RepoLoadError(f"clone target already exists: {dest}")
    if source.kind == "local":
        # Local project folder: copy it instead of `git clone` (no GitHub push needed).
        shutil.copytree(source.location, dest, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"))
        return dest
    cmd = ["git", "clone", "--depth", "1", "--no-recurse-submodules"]
    if source.ref:
        cmd += ["--branch", source.ref]
    cmd += ["--", source.location, dest]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # fail fast on private repos instead of hanging
    if not settings.ALLOW_LOCAL_PATHS:
        env["GIT_ALLOW_PROTOCOL"] = "https"
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True,
                       timeout=settings.CLONE_TIMEOUT, env=env)
    except subprocess.CalledProcessError as e:
        _rmtree(dest)
        err = "\n".join(l for l in e.stderr.strip().splitlines() if not l.startswith("Cloning into"))
        hint = ""
        if "could not read Username" in err or "Authentication failed" in err or "not found" in err.lower():
            hint = " -- check the URL, and note that private repos can't be cloned by this server."
        raise RepoLoadError(f"git clone failed: {err[-600:]}{hint}")
    except subprocess.TimeoutExpired:
        _rmtree(dest)
        raise RepoLoadError(f"git clone timed out after {settings.CLONE_TIMEOUT}s")
    if _dir_size(dest) > settings.MAX_REPO_MB * 1024 * 1024:
        _rmtree(dest)
        raise RepoLoadError(f"the repository is larger than {settings.MAX_REPO_MB} MB; "
                            f"put the AutoCNIC project in a smaller repo.")
    return dest


_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".tox"}


def _find_pipeline_dir(repo_dir: str, subdir: str) -> str:
    """Folder containing autocnic_pipeline.py: the requested subfolder if one
    was given, else the repo root, else the only match within 3 levels."""
    base = os.path.normpath(os.path.join(repo_dir, subdir))
    if not base.startswith(os.path.normpath(repo_dir)) or not os.path.isdir(base):
        raise RepoLoadError(f"folder {subdir!r} does not exist in the repo")
    if os.path.isfile(os.path.join(base, PIPELINE_FILE)):
        return base
    found = []
    for root, dirs, files in os.walk(base):
        depth = os.path.relpath(root, base).count(os.sep) + (root != base)
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS] if depth < 3 else []
        if PIPELINE_FILE in files:
            found.append(root)
    if len(found) == 1:
        return found[0]
    if found:
        rels = ", ".join(os.path.relpath(f, repo_dir).replace(os.sep, "/") for f in found)
        raise RepoLoadError(
            f"found {PIPELINE_FILE} in several folders ({rels}). Paste the GitHub link to the "
            f"one you want (…/tree/<branch>/<folder>)."
        )
    raise RepoLoadError(f"no {PIPELINE_FILE} found in the repo. {GUIDE_HINT}")


def analyze_repo(repo_url: str, n_trials_per_edge: int = 300, seed: int = 0) -> Session:
    """Clones repo_url, starts a worker that loads its pipeline and runs
    discovery (perturbation experiments + predicate mining), and returns a live
    Session with the synthesized CNIC graph and initial state."""
    source = resolve_source(repo_url)
    if not ANALYZE_SLOTS.acquire(timeout=settings.ANALYZE_TIMEOUT):
        raise RepoLoadError("the server is busy analyzing other projects -- try again in a minute.")
    try:
        session_id = uuid.uuid4().hex[:12]
        clone_dir = _clone_repo(source, session_id)
        worker = None
        try:
            repo_dir = _find_pipeline_dir(clone_dir, source.subdir)
            worker = Worker(session_id, repo_dir)
            r = worker.request(
                {"op": "analyze", "repo_dir": repo_dir, "n_trials_per_edge": n_trials_per_edge, "seed": seed},
                timeout=settings.ANALYZE_TIMEOUT)
        except BaseException:
            if worker is not None:
                worker.close()
            _rmtree(clone_dir)
            _remove_quietly(os.path.join(CLONE_ROOT, f"{session_id}.log"))
            raise
    finally:
        ANALYZE_SLOTS.release()

    session = Session(
        id=session_id, repo_url=repo_url, worker=worker, graph=r["graph"], state=r["state"],
        input_nodes=r["input_nodes"], derived_count=r["derived_count"],
        synthesized_desc=r["synthesized_edges"],
    )
    with SESSIONS_LOCK:
        SESSIONS[session_id] = session
    _write_meta(session)
    _evict_old_sessions()
    _prune_stored_sessions()
    return session


def get_live_session(session_id: str) -> Optional[Session]:
    return SESSIONS.get(session_id)


def _drop_session(session: Session):
    """Stop a live session: kill its worker, delete its clone. Its record on
    disk stays and is served read-only."""
    with SESSIONS_LOCK:
        SESSIONS.pop(session.id, None)
    session.worker.close()
    _rmtree(os.path.join(CLONE_ROOT, session.id))
    _remove_quietly(session.worker.log_path)


def _evict_old_sessions():
    """Keep memory bounded: idle sessions, and past the limit the least recently
    used ones, drop to read-only and their worker and clone are removed."""
    now = time.time()
    with SESSIONS_LOCK:
        live = sorted(SESSIONS.values(), key=lambda s: s.last_used)
    excess = len(live) - settings.MAX_LIVE_SESSIONS
    for i, s in enumerate(live):
        if i < excess or now - s.last_used > settings.SESSION_IDLE_SECONDS:
            _drop_session(s)


def shutdown():
    for s in list(SESSIONS.values()):
        _drop_session(s)


# ---- persistence ----------------------------------------------------------

def _remove_quietly(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _cert_store_path(session_id: str) -> str:
    return os.path.join(CERT_STORE_DIR, f"{session_id}.jsonl")


def _meta_path(session_id: str) -> str:
    return os.path.join(CERT_STORE_DIR, f"{session_id}.meta.json")


def _history_path(session_id: str) -> str:
    return os.path.join(CERT_STORE_DIR, f"{session_id}.updates.jsonl")


def _write_meta(session: Session):
    with open(_meta_path(session.id), "w") as f:
        json.dump({
            "session_id": session.id,
            "repo_url": session.repo_url,
            "created_at": session.created_at,
            "graph": session.graph,
            "synthesized_edges": session.synthesized_desc,
            "input_nodes": session.input_nodes,
            "initial_values": session.state["values"],
        }, f, indent=2, default=str)


def _prune_stored_sessions():
    """Keep the disk bounded: delete the oldest non-live session records."""
    metas = sorted(n for n in os.listdir(CERT_STORE_DIR) if n.endswith(".meta.json"))
    if len(metas) <= settings.MAX_STORED_SESSIONS:
        return
    aged = sorted(metas, key=lambda n: os.path.getmtime(os.path.join(CERT_STORE_DIR, n)))
    for name in aged[: len(metas) - settings.MAX_STORED_SESSIONS]:
        sid = name[: -len(".meta.json")]
        if sid in SESSIONS:
            continue
        for p in (_meta_path(sid), _cert_store_path(sid), _history_path(sid)):
            _remove_quietly(p)


def _read_jsonl(path: str) -> List[dict]:
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_certificates(session_id: str) -> List[dict]:
    if not _SESSION_ID.match(session_id):
        return []
    return _read_jsonl(_cert_store_path(session_id))


def load_history(session_id: str) -> List[dict]:
    if not _SESSION_ID.match(session_id):
        return []
    return _read_jsonl(_history_path(session_id))


def load_meta(session_id: str) -> Optional[dict]:
    if not _SESSION_ID.match(session_id):
        return None
    path = _meta_path(session_id)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def list_sessions() -> List[dict]:
    """Every session with metadata on disk (live in memory or not), newest first."""
    out = []
    for name in os.listdir(CERT_STORE_DIR):
        if not name.endswith(".meta.json"):
            continue
        meta = load_meta(name[: -len(".meta.json")])
        if meta is None:
            continue
        sid = meta["session_id"]
        out.append({
            "session_id": sid,
            "repo_url": meta.get("repo_url"),
            "created_at": meta.get("created_at"),
            "n_nodes": len(meta["graph"]["nodes"]),
            "n_edges": len(meta["graph"]["edges"]),
            "n_conditional": sum(1 for e in meta["graph"]["edges"] if e["conditional"]),
            "n_updates": len(load_history(sid)),
            "n_certificates": len(load_certificates(sid)),
            "live": sid in SESSIONS,
        })
    out.sort(key=lambda s: s["created_at"] or 0, reverse=True)
    return out


# ---- updates --------------------------------------------------------------

def apply_update(session: Session, input_node: str, value: Any) -> dict:
    if input_node not in session.input_nodes:
        raise RepoLoadError(f"unknown input node: {input_node}")
    with session.lock:
        if session.id not in SESSIONS:
            raise RepoLoadError("this session was stopped -- re-analyze the repo to continue.")
        session.last_used = time.time()
        try:
            r = session.worker.request(
                {"op": "update", "input_node": input_node, "value": value},
                timeout=settings.UPDATE_TIMEOUT)
        except RepoLoadError as e:
            if session.worker.proc.poll() is not None:
                _drop_session(session)
                raise RepoLoadError(f"{e} This session is now read-only; re-analyze the repo to continue.")
            raise

        now = time.time()
        certs_out = []
        for c in r["certificates"]:
            record = {"cert_id": c["cert_id"], "session_id": session.id, "issued_at": now, **c}
            session.certificates_issued += 1
            if record["verified_ok"]:
                session.certificates_verified_ok += 1
            certs_out.append(record)
        with open(_cert_store_path(session.id), "a") as f:
            for record in certs_out:
                f.write(json.dumps(record, default=str) + "\n")

        baseline_this_update = session.derived_count
        session.cumulative_baseline_invocations += baseline_this_update
        session.cumulative_cnic_invocations += r["model_invocations"]
        session.updates_processed += 1
        session.state = r["state"]

        reduction_pct = 0.0
        if session.cumulative_baseline_invocations:
            reduction_pct = 100.0 * (
                1 - session.cumulative_cnic_invocations / session.cumulative_baseline_invocations
            )

        update_index = session.updates_processed - 1
        with open(_history_path(session.id), "a") as f:
            f.write(json.dumps({
                "update_index": update_index,
                "at": now,
                "input_node": input_node,
                "old_value": r["old_value"],
                "new_value": value,
                "influenced_set": r["influenced_set"],
                "recomputed": r["recomputed"],
                "frontier": r["frontier"],
                "edge_status": r["edge_status"],
                "values": session.state["values"],
                "digest": session.state["digest"],
                "model_invocations": r["model_invocations"],
                "baseline_invocations": baseline_this_update,
                "n_certificates": len(certs_out),
            }, default=str) + "\n")

    return {
        "input_node": input_node,
        "old_value": r["old_value"],
        "update_index": update_index,
        "new_value": value,
        "state": session.state,
        "influenced_set": r["influenced_set"],
        "recomputed": r["recomputed"],
        "frontier": r["frontier"],
        "edge_status": r["edge_status"],
        "certificates_issued": certs_out,
        "model_invocations_this_update": r["model_invocations"],
        "baseline_invocations_this_update": baseline_this_update,
        "cumulative": {
            "updates_processed": session.updates_processed,
            "baseline_invocations": session.cumulative_baseline_invocations,
            "cnic_invocations": session.cumulative_cnic_invocations,
            "reduction_pct": round(reduction_pct, 1),
            "certificates_issued": session.certificates_issued,
            "certificates_verified_ok": session.certificates_verified_ok,
        },
    }
