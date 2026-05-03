"""
CyberLama tool registry. Exposes a small, security-focused toolbox the model can
invoke via OpenAI-style function calling.

Design notes:
- Risky tools (shell_exec, write_file) prompt the user before running unless the
  command is on TOOL_ALLOWLIST and looks unambiguous.
- All tools return strings (truncated when oversized) so they can be fed back to
  the model as `tool` role messages.
- No Windows support: shell + clipboard helpers assume POSIX.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

# ---- ANSI (mirrors cyberlama.py for standalone messages) -------------------
RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
CYAN = "\033[36m"; GRAY = "\033[90m"

# ---- Config ----------------------------------------------------------------
# Commands here run without confirmation. Keep this list to read-only / recon
# tools that an authorized pentester would invoke routinely.
TOOL_ALLOWLIST = {
    "nmap", "curl", "wget", "dig", "whois", "host", "nslookup",
    "ping", "traceroute", "tracepath", "openssl", "nc", "ncat",
    "ls", "cat", "head", "tail", "grep", "find", "file", "stat",
    "wc", "sort", "uniq", "awk", "sed", "id", "uname", "uptime",
    "ip", "ifconfig", "netstat", "ss", "arp", "route",
    "ffuf", "gobuster", "nikto", "sqlmap", "hydra", "john", "hashcat",
    "tshark", "tcpdump", "amass", "subfinder", "httpx", "nuclei",
}

MAX_OUTPUT_BYTES = 16_000  # default truncation fallback
TRUNCATE_LIMITS = {
    "shell_exec": 16_000,
    "shell_session": 16_000,
    "read_file": 64_000,
    "http_get": 32_000,
    "list_dir": 32_000,
    "grep_files": 32_000,
    "recall_journal": 16_000,
}
SHELL_TIMEOUT_SEC = 60
HTTP_TIMEOUT_SEC = 20

# Populated by main module so tools can hit the journal directory.
JOURNAL_DIR: Path | None = None


def configure(*, journal_dir: Path) -> None:
    global JOURNAL_DIR
    JOURNAL_DIR = journal_dir


# ---- Utility ---------------------------------------------------------------
def _truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    if len(s) <= limit:
        return s
    head = s[: limit // 2]
    tail = s[-limit // 2 :]
    omitted = len(s) - len(head) - len(tail)
    return f"{head}\n\n... [truncated {omitted} bytes] ...\n\n{tail}"


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{YELLOW}{prompt} (y/N) > {RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in {"y", "yes"}


def _is_allowlisted(cmd: str) -> bool:
    """First token (resolved via shlex) must be on the allowlist and contain no
    obviously destructive flags."""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts:
        return False
    head = Path(parts[0]).name
    if head not in TOOL_ALLOWLIST:
        return False
    # Refuse a few obviously dangerous patterns even on allowlisted tools.
    bad = (" rm ", " mkfs", "dd if=", ":(){", "shutdown", "reboot")
    flat = " " + cmd + " "
    if any(b in flat for b in bad):
        return False
    return True


# ---- Tool implementations --------------------------------------------------
def shell_exec(cmd: str, confirm: bool = True) -> str:
    """Run a shell command, streaming stdout (with stderr merged) to the
    terminal, and return the full captured output for the model."""
    if confirm and not _is_allowlisted(cmd):
        print(f"{RED}{BOLD}[tool] shell_exec:{RESET} {cmd}")
        if not _confirm("Run this command?"):
            return "ERROR: user declined to run command."
    else:
        print(f"{CYAN}[tool] $ {cmd}{RESET}")

    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True, executable="/bin/bash",
        )
    except Exception as e:
        return f"ERROR: {e}"

    captured: list[str] = []
    start = time.monotonic()
    timed_out = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            captured.append(line)
            print(f"{GRAY}│ {RESET}{line}", end="")
            if time.monotonic() - start > SHELL_TIMEOUT_SEC:
                timed_out = True
                proc.kill()
                break
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return f"ERROR: {e}"

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    if timed_out:
        return f"ERROR: command timed out after {SHELL_TIMEOUT_SEC}s."

    out = "".join(captured) + f"\n--- exit code: {proc.returncode} ---"
    return _truncate(out)


def read_file(path: str, max_bytes: int = MAX_OUTPUT_BYTES) -> str:
    p = _expand(path)
    if not p.exists():
        return f"ERROR: not found: {p}"
    if not p.is_file():
        return f"ERROR: not a regular file: {p}"
    try:
        data = p.read_text(errors="replace")
    except Exception as e:
        return f"ERROR: {e}"
    return _truncate(data, max_bytes)


def write_file(path: str, content: str, confirm: bool = True) -> str:
    p = _expand(path)
    if confirm:
        print(f"{RED}{BOLD}[tool] write_file:{RESET} {p} ({len(content)} bytes)")
        if not _confirm("Write this file?"):
            return "ERROR: user declined to write file."
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    except Exception as e:
        return f"ERROR: {e}"
    return f"OK: wrote {len(content)} bytes to {p}"


def list_dir(path: str = ".") -> str:
    p = _expand(path)
    if not p.exists():
        return f"ERROR: not found: {p}"
    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except Exception as e:
        return f"ERROR: {e}"
    lines = []
    for e in entries[:500]:
        kind = "d" if e.is_dir() else "f"
        try:
            size = e.stat().st_size if e.is_file() else 0
        except Exception:
            size = 0
        lines.append(f"{kind} {size:>10}  {e.name}")
    if len(entries) > 500:
        lines.append(f"... {len(entries) - 500} more entries omitted")
    return "\n".join(lines) or "(empty)"


def http_get(url: str, headers: dict | None = None) -> str:
    if not re.match(r"^https?://", url):
        return "ERROR: only http(s) URLs supported."
    print(f"{CYAN}[tool] GET {url}{RESET}")
    try:
        r = requests.get(url, headers=headers or {}, timeout=HTTP_TIMEOUT_SEC, allow_redirects=True)
    except requests.RequestException as e:
        return f"ERROR: {e}"
    summary = f"HTTP {r.status_code} {r.reason}\nContent-Type: {r.headers.get('Content-Type','?')}\nLength: {len(r.content)}\n\n"
    body = r.text
    ctype = r.headers.get("Content-Type", "")
    if ctype.startswith("text/html"):
        stripped = re.sub(r"<script\b[^>]*>.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r"<style\b[^>]*>.*?</style>", " ", stripped, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        body = "[HTML stripped to text]\n\n" + stripped
    return _truncate(summary + body)


def grep_files(pattern: str, path: str = ".", glob: str = "**/*") -> str:
    p = _expand(path)
    if not p.exists():
        return f"ERROR: not found: {p}"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad regex: {e}"
    hits = []
    targets = [p] if p.is_file() else list(p.glob(glob))
    for f in targets[:5000]:
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}: {line.strip()}")
                    if len(hits) >= 200:
                        hits.append("... 200-hit cap reached")
                        return _truncate("\n".join(hits))
        except Exception:
            continue
    return _truncate("\n".join(hits)) or "(no matches)"


def recall_journal(query: str, limit: int = 5) -> str:
    """Keyword-scored search over ~/.cyberlama/journal/*.log."""
    if JOURNAL_DIR is None or not JOURNAL_DIR.exists():
        return "ERROR: journal not configured."
    terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
    if not terms:
        return "ERROR: query too short."
    scored: list[tuple[int, str, str]] = []
    for log in sorted(JOURNAL_DIR.glob("*.log")):
        try:
            text = log.read_text(errors="replace")
        except Exception:
            continue
        # Score per entry block (separated by 40 dashes).
        for block in text.split("-" * 40):
            low = block.lower()
            score = sum(low.count(t) for t in terms)
            if score:
                scored.append((score, log.name, block.strip()))
    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]
    if not top:
        return "(no matches in journal)"
    out = []
    for score, name, block in top:
        snippet = block[:600] + ("..." if len(block) > 600 else "")
        out.append(f"[{name}] (score={score})\n{snippet}")
    return "\n\n".join(out)


# ---- Persistent shell session ---------------------------------------------
class PersistentShell:
    """A long-lived bash subprocess so `cd`, exports, and shell history persist
    across calls. Uses a unique sentinel marker per command to detect command
    completion and recover the exit code."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["/bin/bash", "-i"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True,
        )

    def run(self, cmd: str, timeout: int = 60) -> str:
        if self.proc.poll() is not None:
            return "ERROR: persistent shell has exited."
        marker = f"__CL_DONE_{uuid.uuid4().hex}__"
        assert self.proc.stdin is not None and self.proc.stdout is not None
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.write(f"echo {marker} $?\n")
            self.proc.stdin.flush()
        except Exception as e:
            return f"ERROR: {e}"

        captured: list[str] = []
        exit_code: str = "?"
        start = time.monotonic()
        timed_out = False
        while True:
            if time.monotonic() - start > timeout:
                timed_out = True
                break
            line = self.proc.stdout.readline()
            if not line:
                break
            if marker in line:
                m = re.search(re.escape(marker) + r"\s+(\S+)", line)
                if m:
                    exit_code = m.group(1)
                break
            captured.append(line)
            print(f"{GRAY}│ {RESET}{line}", end="")

        if timed_out:
            return f"ERROR: command timed out after {timeout}s."
        return "".join(captured) + f"\n--- exit code: {exit_code} ---"

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass


_PERSISTENT_SHELL: PersistentShell | None = None


def _get_persistent_shell() -> PersistentShell:
    global _PERSISTENT_SHELL
    if _PERSISTENT_SHELL is None or _PERSISTENT_SHELL.proc.poll() is not None:
        _PERSISTENT_SHELL = PersistentShell()
    return _PERSISTENT_SHELL


def shell_session(cmd: str) -> str:
    """Run a command in the persistent bash session."""
    print(f"{CYAN}[tool] $$ {cmd}{RESET}")
    sh = _get_persistent_shell()
    return sh.run(cmd, timeout=SHELL_TIMEOUT_SEC)


# ---- DNS lookup ------------------------------------------------------------
def dns_lookup(host: str, record_type: str = "A") -> str:
    rt = record_type.upper()
    if rt in {"A", "AAAA"}:
        family = socket.AF_INET if rt == "A" else socket.AF_INET6
        try:
            infos = socket.getaddrinfo(host, None, family=family)
        except socket.gaierror as e:
            return f"ERROR: {e}"
        addrs = sorted({info[4][0] for info in infos})
        return "\n".join(addrs) if addrs else "(no records)"
    if shutil.which("dig"):
        try:
            proc = subprocess.run(
                ["dig", "+short", host, rt], capture_output=True, text=True,
                timeout=HTTP_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return "ERROR: dig timed out."
        except Exception as e:
            return f"ERROR: {e}"
        out = proc.stdout.strip()
        return out or "(no records)"
    return "ERROR: no resolver available"


# ---- Function-calling schema (OpenAI/llama-server compatible) --------------
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "shell_exec",
            "description": "Run a shell command on the operator's machine and return stdout/stderr. Use for recon, enumeration, and verification (nmap, curl, dig, openssl, etc.). Destructive commands trigger a user confirmation prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Full command line, e.g. 'nmap -sV -p 80,443 target.com'"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a local file (text). Useful for inspecting nmap XML, source code, config files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a local file. Prompts the operator before overwriting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and subdirectories at a path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Issue an HTTP(S) GET and return status + body. Use for fingerprinting endpoints, fetching docs, pulling exploit POCs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_files",
            "description": "Regex-search files under a directory. Returns up to 200 matching lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "default": "**/*"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_journal",
            "description": "Search prior CyberLama session journals for relevant past notes. Use to surface findings from earlier engagements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_session",
            "description": "Run a shell command in a persistent bash session — `cd`, exports, and shell state survive across calls. Use this when you need to chain commands that depend on each other.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dns_lookup",
            "description": "DNS lookup for a host. record_type can be A, AAAA, MX, TXT, NS, CNAME.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "record_type": {"type": "string", "default": "A"},
                },
                "required": ["host"],
            },
        },
    },
]

DISPATCH = {
    "shell_exec": shell_exec,
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "http_get": http_get,
    "grep_files": grep_files,
    "recall_journal": recall_journal,
    "shell_session": shell_session,
    "dns_lookup": dns_lookup,
}


def execute(name: str, raw_args: str) -> str:
    fn = DISPATCH.get(name)
    if fn is None:
        return f"ERROR: unknown tool '{name}'"
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        return f"ERROR: invalid JSON arguments: {e}"
    if not isinstance(args, dict):
        return "ERROR: arguments must be a JSON object"
    try:
        result = fn(**args)
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:
        return f"ERROR: {name} raised {type(e).__name__}: {e}"
    if not isinstance(result, str):
        result = json.dumps(result, default=str)
    return _truncate(result, TRUNCATE_LIMITS.get(name, MAX_OUTPUT_BYTES))


def list_tools() -> str:
    lines = []
    for s in TOOL_SCHEMAS:
        f = s["function"]
        lines.append(f"  {GREEN}{f['name']}{RESET} — {f['description']}")
    return "\n".join(lines)


# ---- Cross-platform clipboard ---------------------------------------------
def copy_to_clipboard(content: str) -> tuple[bool, str | None]:
    """Returns (ok, tool_used). Linux: tries wl-copy then xclip then xsel."""
    candidates: list[tuple[str, list[str]]] = []
    if sys.platform == "darwin":
        candidates.append(("pbcopy", []))
    else:
        if os.environ.get("WAYLAND_DISPLAY"):
            candidates.append(("wl-copy", []))
        candidates.append(("xclip", ["-selection", "clipboard"]))
        candidates.append(("xsel", ["--clipboard", "--input"]))
    for tool, args in candidates:
        if not shutil.which(tool):
            continue
        try:
            subprocess.run([tool] + args, input=content.encode("utf-8"), check=True)
            return True, tool
        except Exception:
            continue
    return False, None
