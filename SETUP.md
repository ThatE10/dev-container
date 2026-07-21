# Claude Code on Modal — setup

Run Claude Code in a GPU-enabled Modal sandbox with a web terminal, using your
Claude.ai OAuth (no API key needed).

## One-time setup

1. **Install and authenticate Modal**
   ```bash
   pip install modal
   python3 -m modal setup
   ```
   (If `modal …` gives `command not found`, pip put the entrypoint in a
   user-scripts dir that isn't on `$PATH`. Using `python3 -m modal …` avoids
   that — every command below uses this form.)

2. **Create the required Modal secrets**

   ```bash
   # Claude Code OAuth (pulled from your macOS Keychain)
   python3 -m modal secret create claude-code-secret \
     CLAUDE_CREDENTIALS="$(security find-generic-password -s 'Claude Code-credentials' -w)"

   # GHCR pull auth — required to pull the private dev-container image
   python3 -m modal secret create ghcr-secret \
     REGISTRY_USERNAME=ThatE10 REGISTRY_PASSWORD=$(gh auth token)
   ```

   Optional, only if you want to clone private GitHub repos into the sandbox:
   ```bash
   python3 -m modal secret create github-secret GH_TOKEN=$(gh auth token)
   ```

## Launching

### Interactive launcher (recommended)

```bash
python launch.py            # menu — pick a saved preset or create one
python launch.py --list     # print presets, most-used first
python launch.py --forget N # delete preset #N
```

Presets are stored at `~/.config/claude-code-modal/presets.json` and sorted by
usage count, so your most-used config is always at the top of the menu. Each
preset stores: name, base image, GPU, CPU, RAM, repo, ref, session hours, and
whether to attach a fresh `gh auth token` on launch.

Tokens are never written to the preset file — the `gh auth token` value is
resolved at launch time.

### Direct invocation

```bash
# Defaults: dev-container image, no GPU, Modal-default CPU/RAM
python claude_code_modal.py

# With a repo, GPU, and larger resources
python claude_code_modal.py \
  --repo owner/repo --ref main \
  --gpu A100-80GB --cpu 8 --memory 32768 --hours 8

# Fast, cheap path — skip the 20GB dev-container image
python claude_code_modal.py --image slim
```

## Secrets attached to the sandbox

Beyond `claude-code-secret` (always) and `ghcr-secret` (for the image pull),
the launcher attaches these Modal secrets by default so the sandbox has your
usual API keys:

- `huggingface-secret` (`HF_TOKEN`) → written to `~/.cache/huggingface/token`
- `github-secret` (`GH_TOKEN` or `GITHUB_TOKEN`) → `gh auth login` runs at startup

Add more via `--secret NAME` (repeatable) on `claude_code_modal.py`, or when
creating a preset the launcher prompts for a comma-separated list. Common
extras: `wandb-secret` (`WANDB_API_KEY`), `openai-secret`, `anthropic-secret`.

On startup the sandbox runs the dev-container's `import-secrets.sh`, which
writes each token to its canonical config location (HF, WandB, Modal, gh).
Pass `--secret ''` once to disable the default secrets entirely.

## Base images

| `--image` value                         | When to use                                          |
|-----------------------------------------|------------------------------------------------------|
| `ghcr.io/thate10/dev-container:latest`  | Default. vLLM + torch + CUDA, plus Claude Code + ttyd. |
| `slim`                                  | Debian slim. Fast pull, CPU only, no torch/CUDA.     |

Any other registry tag also works via `--image <tag> --registry-secret <name>`.

## GPU options

Modal-supported strings: `T4`, `L4`, `A10G`, `L40S`, `A100-40GB`, `A100-80GB`,
`H100`, `H200`. Append `:N` for multi-GPU (e.g. `H100:2`).

Omit `--gpu` for CPU-only sandboxes.

## Troubleshooting

**`Image build … terminated due to external shut-down. Please try again.`**
Transient Modal builder error, typically on the first pull of the ~20GB
dev-container image. The builder's tmp directory can get wiped mid-pull.
Just re-run — subsequent pulls hit Modal's layer cache and complete fast.

**`command not found: modal`** — pip installed the entrypoint outside `$PATH`.
Use `python3 -m modal` instead (all commands in this doc use that form).

**Image pull auth failure** — regenerate `ghcr-secret` with a fresh
`gh auth token`; PATs expire.
