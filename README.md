# CyberLama

**CyberLama** is a specialized, terminal-based LLM client designed for Certified Ethical Hackers (CEH) and cybersecurity professionals. It interfaces with local Llama-models (via `llama-server`) to provide an authorized, offline, and powerful assistant for Red/Blue team operations.

## Features

- **CEH-Focused Modes**: Dedicated prompts for `:lab`, `:recon`, `:defence`, and `:exploit`.
- **Power Tools**:
  - **`:diff`**: Compare generated code against local files.
  - **`:copy`**: One-command clipboard export for code blocks (macOS).
  - **`:compress`**: Smart context summarization for long engagements.
  - **`:once` / `!`**: Ephemeral requests (side-quests) that don't pollute context history.
- **Data Management**:
  - **Auto-Logging**: Daily journals saved to `~/.cyberlama/journal/`.
  - **Prompt Library**: Load complex templates via `:load`.
  - **File Ingestion**: Read local files into context with `:read`.
- **Visuals**:
  - **Streaming Syntax Highlighting**: Real-time coloring for code/narrative.
  - **Markdown Rendering**: Optional rich text support (via `rich`).
  - **Hacker Theme**: Cyber-aesthetic CLI (Magenta/Cyan/Green).

## Installation

1. **Prerequisites**: Python 3.8+ and a running `llama-server` (or compatible OpenAI-API endpoint).
2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

You can configure CyberLama via environment variables or CLI commands.

| Env Variable | Default | Description |
| :--- | :--- | :--- |
| `CYBERLAMA_API_URL` | `http://localhost:8080/v1/chat/completions` | API Endpoint |
| `CYBERLAMA_MODEL` | `Llama-3.1-70B-Instruct...` | Model Name |
| `CYBERLAMA_TEMP` | `0.2` | Temperature (Creativity) |
| `CYBERLAMA_API_KEY` | *(Required)* | API Key (or random string for local) |

## Usage

Start the client:
```bash
python3 cyberlama.py
```

### Key Commands
Type `:help` inside the tool for a full list.

- **Modes**: `:lab` (Default), `:recon`, `:exploit`
- **Actions**:
  - `:read nmap.xml` -> Ingest file.
  - `:load privesc` -> Load `privesc.txt` from templates.
  - `:diff exploit.py` -> Check differences.
  - `:set temp 0.7` -> Increase creativity.
  - `! decode base64...` -> Run without saving to history.

## Directory Structure
CyberLama stores data in `~/.cyberlama/`:
- `history.txt`: Command history (Readline).
- `journal/`: Daily logs of all interactions.
- `engagements/`: Saved session states (JSON).
- `templates/`: Text files loadable via `:load`.
