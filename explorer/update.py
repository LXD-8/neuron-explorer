"""Update this checkout from the author's main branch.

The explorer runs from a git checkout in the normal case, so updates use git:
fetch `lyramakesmusic/neuron-explorer` main into a private ref, then fast-forward
only when the working tree is clean and the histories allow it.  Data directories
(`runs/`, `stats/`, `captures/`, caches, names) are gitignored and are never
touched.

The server exposes the same operations through /api/update/check,
/api/update/apply, /api/update/rollback and /api/update/restart; applying returns
`restart_required` and the page can re-exec the server.  The commit an update
came from is recorded inside the git directory, so `rollback` can put the
checkout back when the new code does not start.
"""

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse

ROOT = os.path.abspath(os.environ.get("EXPLORER_ROOT") or os.path.join(os.path.dirname(__file__), ".."))
UPSTREAM = os.environ.get("EXPLORER_UPSTREAM", "https://github.com/lyramakesmusic/neuron-explorer.git")
BRANCH = os.environ.get("EXPLORER_UPSTREAM_BRANCH", "main")
REF = "refs/remotes/neuron-upstream/" + BRANCH

# Updates overwrite the code that is running, so the source has to be authenticated: https, ssh or a local path.
# Plain http is accepted only for this machine, and `git://`/`ext::`/`fd::` are refused, because they are
# unauthenticated or run a command.
ALLOWED_TRANSPORTS = ("https", "ssh", "file")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class UpdateError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _run(args, timeout=180, check=True):
    try:
        p = subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise UpdateError("update.git_missing", "git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise UpdateError("update.timeout", "git timed out after %s seconds" % timeout)
    if check and p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-4:]
        raise UpdateError("update.git_failed", " / ".join(tail) or "git exited %s" % p.returncode)
    return p


def _git_path(name):
    """A path inside this checkout's git directory: not tracked, never in `git status`, survives restarts."""
    p = _run(["rev-parse", "--git-path", name]).stdout.strip()
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _check_source(url):
    """Refuse upstreams that are not an authenticated transport or a plain path.

    `git fetch` also accepts `ext::<command>`, which runs a command, and a value that starts with `-` would be
    parsed as a git option; both are reachable through EXPLORER_UPSTREAM, so they are rejected before any git call.
    """
    if not url:
        raise UpdateError("update.bad_upstream", "EXPLORER_UPSTREAM is empty")
    if url.startswith("-"):
        raise UpdateError("update.bad_upstream", "EXPLORER_UPSTREAM must not start with '-': %r" % url)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*::", url):  # git's ext::/fd:: transports
        raise UpdateError("update.bad_upstream",
                          "%r uses a git transport that is not allowed; use https, ssh or a local path" % url)
    if "://" in url:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme in ALLOWED_TRANSPORTS:
            return
        if scheme == "http" and parsed.hostname in LOCAL_HOSTS:
            return
        raise UpdateError("update.bad_upstream",
                          "%r is not an allowed transport; use https, ssh or a local path" % url)
    if re.match(r"^[^\s/]+@[^\s/]+:", url):  # scp-like ssh shorthand: git@github.com:owner/repo.git
        return
    if not (os.path.isabs(url) or url.startswith(("./", "../"))):
        raise UpdateError("update.bad_upstream", "%r is neither a URL nor a path" % url)


def _check_branch(branch):
    # the branch name goes into a refspec and a ref name, so keep it to what a branch can actually be called
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", branch or ""):
        raise UpdateError("update.bad_upstream", "EXPLORER_UPSTREAM_BRANCH is not a branch name: %r" % branch)


def _sha(rev):
    return _run(["rev-parse", rev]).stdout.strip()


def _is_ancestor(a, b):
    return _run(["merge-base", "--is-ancestor", a, b], check=False).returncode == 0


def _dirty():
    out = _run(["status", "--porcelain=v1", "--untracked-files=no"], check=False).stdout
    return [line[3:] for line in out.splitlines() if line.strip()][:20]


def _commit_count(current, latest):
    out = _run(["rev-list", "--count", "%s..%s" % (current, latest)]).stdout.strip()
    return int(out) if out.isdigit() else 0


def _repo():
    if not os.path.isdir(os.path.join(ROOT, ".git")) and not os.path.isfile(os.path.join(ROOT, ".git")):
        raise UpdateError("update.not_git",
                          "this explorer is not a git checkout; update it the way it was installed")
    top = _run(["rev-parse", "--show-toplevel"]).stdout.strip()
    if os.path.realpath(top) != os.path.realpath(ROOT):
        raise UpdateError("update.not_git", "the git checkout is at %s, not %s" % (top, ROOT))


def check(fetch=True):
    """Return a JSON-ready status.  Never modifies the working tree."""
    _repo()
    _check_source(UPSTREAM)
    _check_branch(BRANCH)
    if fetch:
        try:
            with _fetch_lock():
                _run(["fetch", "--quiet", "--no-tags", UPSTREAM, "+refs/heads/%s:%s" % (BRANCH, REF)], timeout=60)
        except UpdateError as e:
            if e.code == "update.timeout":
                raise
            if e.code != "update.fetch_busy":  # a racing fetch is fine: it writes the same ref
                raise UpdateError("update.fetch_failed", str(e))
    current = _sha("HEAD")
    try:
        latest = _sha(REF)
    except UpdateError:
        raise UpdateError("update.fetch_failed", "upstream %s/%s was not fetched" % (UPSTREAM, BRANCH))
    branch = _run(["rev-parse", "--abbrev-ref", "HEAD"], check=False).stdout.strip() or "HEAD"
    dirty = _dirty()
    if current == latest:
        return {"ok": True, "available": False, "current": current, "latest": latest,
                "branch": branch, "dirty": dirty, "ahead": False, "fast_forward": False, "count": 0}
    fast_forward = _is_ancestor("HEAD", REF)
    ahead = _is_ancestor(REF, "HEAD")
    return {
        "ok": True,
        "available": not ahead,
        "current": current,
        "latest": latest,
        "branch": branch,
        "dirty": dirty,
        "ahead": ahead,
        "fast_forward": fast_forward,
        "count": _commit_count(current, latest),
    }


def _sync_deps():
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        return False, "uv is not installed; install dependencies with `uv sync` before restarting"
    lock = os.path.join(ROOT, "uv.lock")
    args = [uv, "sync", "--project", ROOT, "--no-progress"]
    if os.path.isfile(lock):
        args.append("--frozen")
    try:
        p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return False, "uv sync timed out after 900 seconds"
    except OSError as e:
        return False, "could not run uv: %s" % e
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-4:]
        return False, "uv sync failed: " + (" / ".join(tail) or "exit %s" % p.returncode)
    return True, ""


def _restore(commit):
    """Put the checkout back on `commit`.  Returns a note for the caller when even that fails, so the error the
    person sees carries the command that finishes the job."""
    p = _run(["reset", "--hard", commit], check=False)
    if p.returncode == 0:
        return ""
    return " (the checkout could not be rolled back automatically; run `git -C %s reset --hard %s`)" % (ROOT, commit)


def _state_path():
    return _git_path("explorer-update.json")


def _read_state():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state):
    path = _state_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# One update at a time, across processes: two explorer instances on the same checkout (or two requests racing in
# one process) must not roll each other back.  flock is on a file in the git directory, so the second one fails
# instead of waiting, and nothing is written to the working tree.
def _lock_file(path, code, message):
    try:
        fh = open(path, "a+")
    except OSError as e:
        raise UpdateError("update.lock_failed", "could not create the update lock %s: %s" % (path, e))
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise UpdateError(code, message)
    return fh


def acquire_lock():
    """The apply/rollback lock.  The caller must drop_lock() it; /api/update/restart holds it until execve."""
    return _lock_file(_git_path("explorer-update.lock"), "update.busy",
                      "another update is already running; wait for it to finish")


def drop_lock(fh):
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


@contextlib.contextmanager
def _update_lock():
    fh = acquire_lock()
    try:
        yield
    finally:
        drop_lock(fh)


@contextlib.contextmanager
def _fetch_lock():
    """A fetch writes refs/remotes/neuron-upstream/main, and a second one racing it makes git fail on the ref lock.
    There is nothing to wait for — the ref ends up the same — so a concurrent check just reports what is there."""
    fh = _lock_file(_git_path("explorer-update-fetch.lock"), "update.fetch_busy", "another fetch is running")
    try:
        yield
    finally:
        drop_lock(fh)


def apply_update():
    """Fast-forward to upstream and reconcile dependencies.  Refuses to touch a dirty tree."""
    _repo()
    with _update_lock():
        info = check()
        if not info["available"]:
            return {"ok": True, "up_to_date": True, "commit": info["current"], "restart_required": False}
        if info["dirty"]:
            raise UpdateError("update.dirty",
                              "the working tree has changes; commit or stash them before updating: " +
                              ", ".join(info["dirty"][:5]))
        if not info["fast_forward"]:
            raise UpdateError("update.diverged",
                              "local commits diverged from %s/%s; merge or rebase manually" % (UPSTREAM, BRANCH))
        previous = info["current"]
        p = _run(["merge", "--ff-only", REF], timeout=180, check=False)
        if p.returncode != 0:
            raise UpdateError("update.apply_failed", (p.stderr or p.stdout).strip()[-800:])
        commit = _sha("HEAD")
        if commit != info["latest"]:
            # git reported success but the checkout is not on the fetched tip: do not leave that in place.
            note = _restore(previous) or "; the update was rolled back"
            raise UpdateError("update.apply_failed",
                              "the checkout is at %s after the merge, not %s%s" % (commit, info["latest"], note))
        _write_state({"from": previous, "to": commit, "at": int(time.time())})
        deps_ok, deps_error = _sync_deps()
        if not deps_ok:
            # Keep the old code runnable: restore the pre-update commit and report the dependency failure.
            note = _restore(previous) or "; the update was rolled back"
            raise UpdateError("update.deps_failed", (deps_error or "dependency sync failed") + note)
        return {"ok": True, "up_to_date": False, "commit": commit, "previous": previous,
                "restart_required": True, "deps_ok": True, "deps_error": ""}


def rollback_update():
    """Undo the last applied update, for when the new code does not start or a bug is in the way."""
    _repo()
    with _update_lock():
        state = _read_state()
        previous, current = state.get("from"), state.get("to")
        if not previous or not current:
            raise UpdateError("update.no_rollback", "there is no recorded update to roll back")
        head = _sha("HEAD")
        if head == previous:
            return {"ok": True, "up_to_date": True, "commit": head, "restart_required": False}
        if head != current:
            raise UpdateError("update.no_rollback",
                              "the checkout is at %s, not at the commit the last update installed (%s); "
                              "roll back by hand" % (head[:12], current[:12]))
        if not _is_ancestor(previous, head):
            raise UpdateError("update.no_rollback", "%s is not an ancestor of %s; roll back by hand"
                              % (previous[:12], head[:12]))
        dirty = _dirty()
        if dirty:
            raise UpdateError("update.dirty",
                              "the working tree has changes; commit or stash them before rolling back: " +
                              ", ".join(dirty[:5]))
        p = _run(["reset", "--hard", previous], check=False)
        if p.returncode != 0:
            raise UpdateError("update.apply_failed", (p.stderr or p.stdout).strip()[-800:])
        deps_ok, deps_error = _sync_deps()
        if not deps_ok:
            # the rollback did not work out: stay on the version that was running
            note = _restore(head) or "; the rollback was undone"
            raise UpdateError("update.deps_failed", (deps_error or "dependency sync failed") + note)
        _write_state({**state, "rolled_back": int(time.time())})
        return {"ok": True, "up_to_date": False, "commit": previous, "previous": head,
                "restart_required": True, "deps_ok": True, "deps_error": ""}


def status():
    """What /api/update/status adds: the boot id comes from the server, this is what the last update recorded."""
    try:
        state = _read_state()
    except UpdateError:  # not a git checkout: the page's restart poll still has to answer
        state = {}
    return {"previous": state.get("from") or "", "updated_at": state.get("at") or 0,
            "rolled_back_at": state.get("rolled_back") or 0}
