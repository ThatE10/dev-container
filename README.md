# Dev Container

A GPU dev container you can **spin up, code in from your browser, save, and
relaunch later with everything intact** — your project files, editor extensions,
installed packages, shell history, and Claude Code setup.

Built on `vllm/vllm-openai` (PyTorch + CUDA kernels prebuilt) with **VS Code in
the browser (code-server)**, JupyterLab, Marimo, vLLM, and the Claude Code CLI.

---

## Requirements

- Docker + Docker Compose v2
- An **NVIDIA GPU** with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  (the container reserves a GPU and won't start without one)

## Quick start

```bash
cp .env.example .env      # then fill in the values you need
make up                   # build/start in the background
```

Open **http://localhost:8443** → full VS Code in your browser, already pointed at
your workspace.

Stop when you're done:

```bash
make down                 # container is removed; your context is kept
```

Come back later with `make up` — your project, extensions, and history are all
still there.

Run `make help` to see every command (`up`, `down`, `shell`, `logs`, `rebuild`,
`new`, `status`, …).

## Start a new project

```bash
make new NAME=my-project
```

Creates `workspace/my-project/` with a git repo and a `.venv`. Open it in VS Code
at http://localhost:8443 and start coding. Install project deps into the venv:

```bash
cd /root/workspace/my-project
source .venv/bin/activate
pip install <whatever>
```

Because `./workspace` is a **host bind mount**, everything under it — including
each project's `.venv` — lives on your machine and persists automatically.

## What persists (and what doesn't)

Everything you care about survives `make down` **and** a full `make rebuild`:

| Persisted                                   | Where it's stored            |
| ------------------------------------------- | ---------------------------- |
| Project code + per-project `.venv`s         | `./workspace` (host bind)    |
| VS Code extensions & settings               | `dev-local` volume           |
| `pip install --user` packages, `~/.local/bin` | `dev-local` volume         |
| Shell history                               | `dev-local` volume           |
| Claude Code config & chat history           | `claude-data` volume         |
| Downloaded model weights (HuggingFace)      | `hf-cache` volume            |

**Not** persisted (rebuilt from the image each time): system packages installed
at runtime with `apt-get`, and any files written outside the paths above. To make
a system-level change stick, add it to the `Dockerfile` and `make rebuild`.

> **Tip:** install Python packages you want to keep with `pip install --user`
> (goes to the persisted `dev-local` volume) or into a project `.venv` under
> `./workspace`. A bare `pip install` writes into the image layer and is lost on
> rebuild.

## Ports

| Port | Service                              | URL / usage                         |
| ---- | ------------------------------------ | ----------------------------------- |
| 8443 | code-server (browser VS Code)        | http://localhost:8443               |
| 2222 | SSH                                  | `ssh root@localhost -p 2222`        |
| 8888 | JupyterLab                           | run `jupyter lab` in the container  |
| 2719 | Marimo                               | run `marimo edit` in the container  |
| 8080 | vLLM OpenAI-compatible API / general | run the vLLM server in the container|

code-server auto-starts; the others you launch from a terminal (in VS Code or
over SSH) when you need them.

## Security note

By default the browser VS Code runs with **auth disabled** and binds to all
interfaces, matching the JupyterLab setup — fine on `localhost` or behind an SSH
tunnel. If the host is reachable by others, set a password in `.env`:

```bash
CODE_SERVER_PASSWORD=some-strong-password
```

Or tunnel instead of exposing the port: `ssh -N -L 8443:localhost:8443 root@host -p 2222`.

## Secrets

`make up` reads `.env`. Populate only what you use — SSH auth, `HF_TOKEN`,
`ANTHROPIC_API_KEY`, `CLAUDE_CREDENTIALS`, Modal, W&B, GitHub. See
[`.env.example`](.env.example) for the full list and where to get each value.

## Reset your context

To wipe the persisted editor/tool/Claude context (but keep your `./workspace`
code):

```bash
make clean-context
```
