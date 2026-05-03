"""
CyberLama recon flows. Chained shell-command sequences the operator can trigger
with a single slash command (e.g. `:scan target.com`).

Each flow runs a series of read-only recon tools and bundles the output for the
LLM to analyze. Steps whose required binary is missing from PATH are reported as
skipped rather than failing the whole flow.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Callable

# ---- Config ----------------------------------------------------------------
MAX_STEP_OUTPUT = 4000  # per-step truncation before bundling for the model


# ---- Data model ------------------------------------------------------------
@dataclass(frozen=True)
class FlowStep:
    label: str             # human-readable description
    cmd_template: str      # shell command, with `{target}` placeholder
    required_tool: str     # binary name to look up via shutil.which


@dataclass(frozen=True)
class Flow:
    name: str
    description: str
    steps: list[FlowStep] = field(default_factory=list)


# ---- Registry --------------------------------------------------------------
FLOWS: dict[str, Flow] = {
    "scan": Flow(
        name="scan",
        description="Quick target recon: resolve, top-100 ports, whois.",
        steps=[
            FlowStep("Resolve host -> IP",   "dig +short {target}",                       "dig"),
            FlowStep("Top 100 TCP ports",    "nmap -sV -Pn -T4 -F {target}",              "nmap"),
            FlowStep("WHOIS (first 40 lines)", "whois {target} | head -40",               "whois"),
        ],
    ),
    "web": Flow(
        name="web",
        description="Web-focused recon: headers, robots.txt, HTTP service scan.",
        steps=[
            FlowStep("Response headers",     "curl -sI -L --max-time 10 https://{target}", "curl"),
            FlowStep("robots.txt",           "curl -sL --max-time 10 https://{target}/robots.txt", "curl"),
            FlowStep("HTTP service scan",
                     "nmap -sV -Pn -p 80,443,8080,8443 --script=http-title,http-headers {target}",
                     "nmap"),
        ],
    ),
    "dns": Flow(
        name="dns",
        description="DNS enumeration: A, AAAA, MX, TXT, NS records.",
        steps=[
            FlowStep("A records",    "dig +short {target} A",    "dig"),
            FlowStep("AAAA records", "dig +short {target} AAAA", "dig"),
            FlowStep("MX records",   "dig +short {target} MX",   "dig"),
            FlowStep("TXT records",  "dig +short {target} TXT",  "dig"),
            FlowStep("NS records",   "dig +short {target} NS",   "dig"),
        ],
    ),
}


# ---- Utility ---------------------------------------------------------------
def _truncate(s: str, limit: int = MAX_STEP_OUTPUT) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "..."


# ---- Public API ------------------------------------------------------------
def available_flows() -> list[tuple[str, str]]:
    """Return [(name, description), ...] sorted by name for `:flows` listings."""
    return sorted(((f.name, f.description) for f in FLOWS.values()), key=lambda x: x[0])


def run_flow(
    name: str,
    target: str,
    runner: Callable[[str], str],
) -> list[tuple[str, str, str]]:
    """Execute the named flow against `target`.

    `runner(cmd) -> str` is supplied by the caller (typically a closure over
    tools.shell_exec with confirm=False). Returns a list of
    `(label, cmd, output)` tuples — one per step, including skipped steps.
    """
    flow = FLOWS.get(name)
    if flow is None:
        return [("error", "", f"ERROR: unknown flow '{name}'")]

    results: list[tuple[str, str, str]] = []
    for step in flow.steps:
        cmd = step.cmd_template.format(target=target)
        if not shutil.which(step.required_tool):
            results.append((
                step.label, cmd,
                f"[skipped: required tool '{step.required_tool}' not installed]",
            ))
            continue
        try:
            output = runner(cmd)
        except Exception as e:
            output = f"ERROR: {type(e).__name__}: {e}"
        results.append((step.label, cmd, _truncate(output)))
    return results


def format_flow_result(target: str, results: list[tuple[str, str, str]]) -> str:
    """Render flow results as a single string for injection back into the chat."""
    parts = [f"=== Flow results for {target} ===", ""]
    for label, cmd, output in results:
        parts.append(f"## {label} ($ {cmd})")
        parts.append(output)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
