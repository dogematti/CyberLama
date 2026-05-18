# CyberLama

**CyberLama** is a terminal-based LLM agent for Certified Ethical Hackers and security professionals. It talks to a local `llama-server` (or any OpenAI-compatible endpoint) and gives the model real tools — shell, HTTP, file I/O, DNS, persistent bash sessions — so it can actually run scans, fetch endpoints, and analyze results instead of just lecturing about them.

> Linux and macOS only. No Windows support.

## What it does

- **Real tool use.** The model can call `shell_exec`, `shell_session` (persistent bash), `read_file`, `write_file`, `list_dir`, `http_get`, `grep_files`, `dns_lookup`, and `recall_journal`. Allowlisted recon tools (nmap, curl, dig, ffuf, …) skip confirmation; anything else prompts.
- **Auto-run literal commands.** Type `nmap -sV target.com` directly — if the first token is on the allowlist, CyberLama auto-routes it to `:run` and feeds the output back into the conversation.
- **Recon flow macros.** `:scan target`, `:web target`, `:dns target` chain several recon tools and bundle the output for the model to analyze.
- **Background tasks.** `:bg <cmd>` fires off long scans in the background; `:bg fetch <id>` pulls the result into context when ready. State persists across restarts.
- **Targets registry.** `:target add prod 10.0.0.5 "billing app"` saves named targets that get injected into the system prompt.
- **Engagements.** Switchable saved sessions (`:engage red-2026-q2`), each with its own message history, memory, and notes.
- **Journal recall.** `:recall pivot` keyword-searches every prior session log.
- **Engagement debrief.** `:summary` asks the model to write a structured Targets / Findings / Commands / Outstanding report.
- **Streaming output.** Long shell commands stream live (line by line with a `│ ` prefix) instead of blocking until done.
- **Tab completion** for slash commands, engagement names, template names, target names, and background task IDs.
- **Cross-platform clipboard** (`pbcopy` / `wl-copy` / `xclip` / `xsel`).
- **Auto-compress** when prompt tokens exceed 80% of the context window.

## Install

```bash
pip install -r requirements.txt
```

Python 3.10+. Optional clipboard helper on Linux: `xclip`, `xsel`, or `wl-copy`.

## Run llama-server

```bash
llama-server \
  -m ./gemma-4-31B-it-uncensored-Q8_0.gguf \
  -c 16384 -ngl 999 \
  --host 127.0.0.1 --port 8080 --jinja
```

`--jinja` enables the model's chat template — this is what makes `llama-server` emit native OpenAI-style `tool_calls`. If the model/template doesn't expose tools, CyberLama's prompt-based fallback (`<tool_call>{...}</tool_call>` blocks) handles it.

The llama.cpp server binary is normally `llama-server`; if your install exposes it as `lama-server`, use that binary name with the same flags.

Then in another terminal:

```bash
python3 cyberlama.py
```

## Configuration

Env vars take precedence over `~/.cyberlama/config.json` over defaults.

| Env Variable | Default | Description |
| :--- | :--- | :--- |
| `CYBERLAMA_API_URL` | `http://localhost:8080/v1/chat/completions` | API endpoint |
| `CYBERLAMA_MODEL` | `gemma-4-31B-it-uncensored-Q8_0.gguf` | Model name sent to the server |
| `CYBERLAMA_TEMP` | `0.2` | Sampling temperature |
| `CYBERLAMA_RENDER` | `true` | Render assistant output as Markdown (needs `rich`) |
| `CYBERLAMA_TOOLS` | `true` | Expose function tools to the model |
| `CYBERLAMA_TOOL_RESULT_PROTOCOL` | `text` | Feed tool results back as `<tool_result>` text (`text`) or OpenAI `tool` role messages (`native`) |
| `CYBERLAMA_AUTO_COMPRESS` | `true` | Auto-summarize history when context fills |
| `CYBERLAMA_AUTO_RUN_SHELL` | `true` | Auto-route literal allowlisted commands to `:run` |
| `CYBERLAMA_CONTEXT_WINDOW` | `16384` | Token budget for ctx meter / auto-compress trigger |
| `CYBERLAMA_API_KEY` | *(optional)* | Only needed if your endpoint enforces auth |

`~/.cyberlama/config.json` example:
```json
{
  "api_url": "http://127.0.0.1:8080/v1/chat/completions",
  "model": "gemma-4-31B-it-uncensored-Q8_0.gguf",
  "temp": 0.3,
  "tools": true,
  "context_window": 16384
}
```

## Tools the model can call

| Tool | What it does |
| :--- | :--- |
| `shell_exec(cmd)` | Run a one-shot shell command, streaming output. |
| `shell_session(cmd)` | Run in a persistent bash session — `cd`, exports, env survive across calls. |
| `read_file(path)` | Read a text file. |
| `write_file(path, content)` | Write a file (operator confirms). |
| `list_dir(path)` | List a directory. |
| `http_get(url, headers?)` | HTTP(S) GET; HTML responses get tag-stripped to text. |
| `grep_files(pattern, path?, glob?)` | Regex search across files. |
| `dns_lookup(host, record_type?)` | DNS resolution (A/AAAA/MX/TXT/NS/CNAME). |
| `recall_journal(query, limit?)` | Keyword search of prior CyberLama session journals. |

Manage tool execution:
```
:tools                # show registry + allowlist
:tools off            # disable model tool calling entirely
:tools allow ffuf     # add a binary to the no-confirm allowlist
:tools deny nikto     # remove from allowlist
```

## Recon flows

```
:scan scanme.nmap.org   # dig + nmap top-100 + whois
:web  scanme.nmap.org   # curl headers + robots.txt + http nmap scripts
:dns  scanme.nmap.org   # A / AAAA / MX / TXT / NS records
:flow scan target       # generic flow runner
:flows                  # list available flows
```

Each flow runs its steps via `shell_exec` (no confirm — they're allowlisted recon tools), bundles the output as a `<tool_result>` block, and appends it to the conversation. The model sees the raw output and you can ask it to analyze.

## Background tasks

```
:bg nmap -p- -T4 target.com    # start; returns immediately with task id
:bg                             # list tasks (running / done / killed)
:bg fetch 20260503-141416-7dc   # pull a finished task's log into context
:bg kill <id>                   # SIGTERM (then SIGKILL after 2s)
:bg cleanup                     # prune old finished tasks (keeps last 20)
```

State persists in `~/.cyberlama/bg_tasks/`. Tasks survive client restarts; on restart, any "running" task whose pid is gone is reconciled to "killed".

## Targets registry

```
:target add prod-web 10.0.0.5 internal billing app
:target add scanme   scanme.nmap.org demo target
:targets                       # list saved targets
:target rm prod-web
```

Saved targets get injected into the system prompt as `Known targets:` lines, so the model can reference them by short name.

## Engagements

```
:engage red-2026-q2     # switch (or create) an engagement
:engagements            # list all
:remember the SSH key for jumpbox is ~/.ssh/jump.pem
:memory                 # show stored facts
:notes phase 1 complete; pivoting to internal subnet
:notes                  # show all notes for current engagement
:summary                # LLM-generated debrief (saved to engagement dir)
:export report.md       # full conversation as Markdown
```

## Direct execution / escape hatches

- `:run <cmd>` — execute via `tools.shell_exec`, output fed back into history.
- `nmap target` (or any allowlisted binary) — auto-routes to `:run` unless prefixed with `:` or `!`.
- `:exec <cmd>` — confirm-then-shell-out (legacy, no journaling).
- `! <prompt>` — ephemeral chat turn (not saved to history).

## Other commands

| Command | Purpose |
| :--- | :--- |
| `:health` | Ping the LLM endpoint (`/health` and `/v1/models`). |
| `:env` | Show resolved config. |
| `:status` | Mode, phase, depth, latency, tokens/sec. |
| `:set temp 0.7` | Live-tune temperature, model, render. |
| `:taylor` | Switch to the Taylor remote endpoint using the default Gemma model and 16k context. |
| `:diff a.txt b.txt` | Diff two files (or file-vs-last-codeblock with one arg). |
| `:copy [n]` | Copy code block #n to clipboard. |
| `:compress` | Manually summarize older history. |
| `:retry` | Drop last assistant turn, regenerate. |
| `:recall <q>` | Search prior session journals. |
| `:reset` | Clear conversation (keeps engagement + memory + targets). |
| `:q` | Quit. |

## Directory layout

```
~/.cyberlama/
├── config.json          # optional persistent config
├── targets.json         # named targets registry
├── history.txt          # readline command history
├── journal/             # daily logs (one file per date)
├── engagements/<name>/  # per-engagement state
│   ├── messages.json    # conversation history
│   ├── memory.json      # remembered facts
│   ├── notes.md         # operator notes
│   └── debrief_*.md     # generated summaries
├── bg_tasks/            # background task metadata + logs
└── templates/           # *.txt prompts loadable via :load
```

## Troubleshooting

**Model lectures instead of running tools.** Common with narrowly fine-tuned models. Either swap to a more capable instruction-tuned model (Gemma 4, Llama 3.x Instruct, Qwen 2.5), or just type the literal shell command — auto-run handles it.

**`unknown model architecture` error in llama-server.** Your llama.cpp build is too old for the model. `brew upgrade llama.cpp` or build from source on the master branch.

**Tools not firing despite `--jinja`.** The model's chat template doesn't expose tools cleanly. The text-based fallback handles it — you'll see `» tool_name(...)` in the transcript when it kicks in.
