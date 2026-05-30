---
title: Installation
description: Get OpenJarvis running on Windows — desktop app, CLI, or Python SDK
search:
  boost: 3
---

# Installation (Windows)

This fork is **Windows-only**. The upstream OpenJarvis project targets macOS,
Linux, and WSL2 — see [open-jarvis/OpenJarvis](https://github.com/open-jarvis/OpenJarvis)
if you need those platforms.

OpenJarvis runs entirely on your hardware. Pick the interface that fits your
workflow.

---

## Desktop App

A native Windows window for the OpenJarvis chat UI. The app is a UI shell that
talks to a backend running on your machine — install both.

### Step 1. Install prerequisites

Open **PowerShell as Administrator** and run:

```powershell
# Python package & project manager
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Rust toolchain (required to build the openjarvis_rust extension)
winget install --id Rustlang.Rustup -e

# Node.js 20+ (required for the browser UI and Tauri build)
winget install --id OpenJS.NodeJS.LTS -e
```

Then close and reopen PowerShell so the new `PATH` entries take effect.

You also need the **MSVC linker** for Rust to compile. Install **Visual Studio
Build Tools 2022** with the "Desktop development with C++" workload from
[visualstudio.microsoft.com](https://visualstudio.microsoft.com/downloads/).

### Step 2. Install the backend

```powershell
git clone https://github.com/pokeyt59/OpenJarvis-Windows-focused.git
cd OpenJarvis-Windows-focused
uv sync --extra server
uv run maturin develop --manifest-path rust/crates/openjarvis-python/Cargo.toml
```

On Python 3.14+ only, prefix the `maturin` command with
`$env:PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1; `.

### Step 3. Start the inference backend

OpenJarvis needs a local model runner. The easiest is
[Ollama](https://ollama.com).

```powershell
# Install Ollama (one-time)
winget install --id Ollama.Ollama -e

# In a dedicated PowerShell window, start the server and pull a small model
ollama serve
ollama pull qwen3:0.6b
```

### Step 4. Start the OpenJarvis backend

In another PowerShell window at the repo root:

```powershell
uv run jarvis serve --port 8000
```

### Step 5. Launch the desktop app

Download the installer from the [GitHub Releases](https://github.com/pokeyt59/OpenJarvis-Windows-focused/releases)
page (file name: `OpenJarvis_x.y.z_x64-setup.exe`) and double-click it. The
desktop window connects to `http://localhost:8000` automatically.

---

## CLI

For terminal use without the desktop window, the steps above give you the
`jarvis` command for free:

```powershell
uv run jarvis --version
uv run jarvis ask "What is the capital of France?"
uv run jarvis chat
uv run jarvis doctor   # diagnoses your install
uv run jarvis model list
```

The CLI requires a running inference backend (Ollama, vLLM, llama.cpp, or
a cloud API). See [Setting up an inference backend](#inference-backends) below.

---

## Python SDK

```python
from openjarvis import Jarvis

j = Jarvis()
print(j.ask("Explain quicksort in two sentences."))
j.close()
```

See the [Python SDK guide](../user-guide/python-sdk.md) for the full API
reference.

---

## Requirements summary

| Requirement | Version | Install |
|-------------|---------|---------|
| Windows | 10 (build 19041+) or 11 | — |
| Python | 3.10+ | [python.org](https://www.python.org/downloads/) |
| uv | latest | `irm https://astral.sh/uv/install.ps1 \| iex` |
| Git | any | `winget install --id Git.Git` |
| Rust | stable | `winget install --id Rustlang.Rustup` |
| Visual Studio Build Tools | 2022 | [visualstudio.microsoft.com](https://visualstudio.microsoft.com/downloads/) (with "Desktop development with C++") |
| Node.js | 20+ | `winget install --id OpenJS.NodeJS.LTS` |
| Inference backend | any | See below |
| Docker Desktop | optional | Needed for the SearXNG web-search connector; see [Connectors](../user-guide/channels-and-connectors.md) |

---

## Optional Extras

OpenJarvis uses optional extras to keep the base installation lightweight.

### Inference Backends

| Extra | Install Command | Description |
|-------|-----------------|-------------|
| `inference-cloud` | `uv sync --extra inference-cloud` | OpenAI and Anthropic APIs |
| `inference-google` | `uv sync --extra inference-google` | Google Gemini API |

Ollama and llama.cpp talk to OpenJarvis over HTTP — no extra Python deps
needed, just have the engine running.

### Memory Backends

| Extra | Install Command | Description |
|-------|-----------------|-------------|
| `memory-faiss` | `uv sync --extra memory-faiss` | FAISS vector store |
| `memory-bm25` | `uv sync --extra memory-bm25` | BM25 sparse retrieval |

The default SQLite/FTS5 memory backend requires no additional dependencies.

### Server & Other

| Extra | Install Command | Description |
|-------|-----------------|-------------|
| `server` | `uv sync --extra server` | OpenAI-compatible API server (`jarvis serve`) |
| `dev` | `uv sync --extra dev` | Development and testing tools |
| `docs` | `uv sync --extra docs` | Documentation build tools |

Combine extras:

```powershell
uv sync --extra server --extra memory-faiss --extra inference-cloud
```

---

## Inference Backends

OpenJarvis requires at least one inference backend. Choose based on your
hardware.

### Ollama (Recommended)

```powershell
winget install --id Ollama.Ollama -e
ollama serve
ollama pull qwen3:0.6b
uv run jarvis model list
```

Best for: consumer NVIDIA GPUs (CUDA), CPU-only systems.

### llama.cpp

Efficient CPU + GPU inference with GGUF quantized models. Build or download
prebuilt binaries from [github.com/ggerganov/llama.cpp](https://github.com/ggerganov/llama.cpp).
Start with:

```powershell
.\llama-server.exe -m C:\path\to\model.gguf --port 8080
```

OpenJarvis auto-detects llama.cpp at `http://localhost:8080`.

### Cloud APIs

```powershell
uv sync --extra inference-cloud --extra inference-google
$env:OPENAI_API_KEY = "sk-..."
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

---

## Next Steps

- [Quick Start](quickstart.md) — Run your first query
- [Configuration](configuration.md) — Customize engine hosts, model routing,
  memory, and more
