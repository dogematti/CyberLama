"""
CyberLama background task manager. Runs long shell commands (full nmap scans,
ffuf grinds, etc.) detached from the chat loop so the operator can fire them
off, keep working, and pull results back when ready.

Persists task metadata + logs under ~/.cyberlama/bg_tasks/ so state survives
restarts. On reload, any "running" task whose pid is gone is reconciled to
"killed".
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

MAX_LOG_BYTES = 16_000


# ---- Utility ---------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_task_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = os.urandom(2).hex()[:3]
    return f"{stamp}-{suffix}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _truncate(s: str, limit: int = MAX_LOG_BYTES) -> str:
    if len(s) <= limit:
        return s
    head = s[: limit // 2]
    tail = s[-limit // 2 :]
    omitted = len(s) - len(head) - len(tail)
    return f"{head}\n\n... [truncated {omitted} bytes] ...\n\n{tail}"


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


# ---- Data ------------------------------------------------------------------
@dataclass
class BackgroundTask:
    id: str
    cmd: str
    status: str  # "running" | "done" | "killed" | "error"
    started_at: str
    log_path: str
    ended_at: str | None = None
    exit_code: int | None = None
    pid: int | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "BackgroundTask":
        data: dict[str, Any] = json.loads(raw)
        return cls(**data)


# ---- Manager ---------------------------------------------------------------
class BackgroundManager:
    """Tracks fire-and-forget shell tasks. Thread-safe for the small set of
    operations the chat loop performs."""

    def __init__(self, dir: Path):
        self.dir = Path(dir).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._tasks: dict[str, BackgroundTask] = {}
        self._load_existing()

    # ---- persistence ------------------------------------------------------
    def _meta_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _log_path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.log"

    def _persist(self, task: BackgroundTask) -> None:
        _atomic_write(self._meta_path(task.id), task.to_json())

    def _load_existing(self) -> None:
        for meta in self.dir.glob("*.json"):
            try:
                task = BackgroundTask.from_json(meta.read_text())
            except Exception:
                continue
            # Reconcile crashed-while-running tasks.
            if task.status == "running" and (task.pid is None or not _pid_alive(task.pid)):
                task.status = "killed"
                task.ended_at = task.ended_at or _now_iso()
                self._persist(task)
            self._tasks[task.id] = task

    # ---- lifecycle --------------------------------------------------------
    def start(self, cmd: str) -> BackgroundTask:
        task_id = _new_task_id()
        log_path = self._log_path(task_id)
        log_fh = open(log_path, "wb")
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                executable="/bin/bash",
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_fh.close()
        task = BackgroundTask(
            id=task_id,
            cmd=cmd,
            status="running",
            started_at=_now_iso(),
            log_path=str(log_path),
            pid=proc.pid,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._persist(task)
        return task

    def list(self) -> list[BackgroundTask]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.started_at, reverse=True)

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def poll(self, task_id: str) -> BackgroundTask | None:
        task = self._tasks.get(task_id)
        if task is None or task.status != "running" or task.pid is None:
            return task
        if _pid_alive(task.pid):
            return task
        # Process gone — try to reap exit code via waitpid (non-blocking).
        exit_code: int | None = None
        try:
            pid, status = os.waitpid(task.pid, os.WNOHANG)
            if pid == task.pid:
                if os.WIFEXITED(status):
                    exit_code = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    exit_code = -os.WTERMSIG(status)
        except ChildProcessError:
            pass
        except OSError:
            pass
        with self._lock:
            task.status = "error" if exit_code not in (0, None) else "done"
            if exit_code is None:
                task.status = "done"
            task.exit_code = exit_code
            task.ended_at = _now_iso()
            self._persist(task)
        return task

    def poll_all(self) -> None:
        for tid in list(self._tasks.keys()):
            t = self._tasks[tid]
            if t.status == "running":
                self.poll(tid)

    def fetch(self, task_id: str, max_bytes: int = MAX_LOG_BYTES) -> str:
        task = self._tasks.get(task_id)
        if task is None:
            return f"ERROR: no such task {task_id}"
        log = Path(task.log_path)
        if not log.exists():
            return "(no log yet)"
        try:
            data = log.read_text(errors="replace")
        except Exception as e:
            return f"ERROR: {e}"
        return _truncate(data, max_bytes)

    def kill(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.pid is None:
            return False
        if not _pid_alive(task.pid):
            with self._lock:
                if task.status == "running":
                    task.status = "killed"
                    task.ended_at = _now_iso()
                    self._persist(task)
            return True
        try:
            os.kill(task.pid, signal.SIGTERM)
        except OSError:
            return False
        for _ in range(20):
            if not _pid_alive(task.pid):
                break
            time.sleep(0.1)
        if _pid_alive(task.pid):
            try:
                os.kill(task.pid, signal.SIGKILL)
            except OSError:
                pass
        with self._lock:
            task.status = "killed"
            task.ended_at = _now_iso()
            self._persist(task)
        return True

    def cleanup(self, keep_last: int = 20) -> int:
        finished = [t for t in self._tasks.values() if t.status != "running"]
        finished.sort(key=lambda t: t.ended_at or t.started_at, reverse=True)
        stale = finished[keep_last:]
        removed = 0
        with self._lock:
            for task in stale:
                for path in (self._meta_path(task.id), Path(task.log_path)):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except Exception:
                        continue
                self._tasks.pop(task.id, None)
                removed += 1
        return removed
