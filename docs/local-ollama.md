# Running locally with Ollama (no paid APIs)

The reviewer can run entirely on your machine: Ollama serves a local model, the app talks to it
through the same provider-neutral layer as Anthropic/OpenAI (`LLM_PROVIDER=ollama`).

## Prerequisites

- [Ollama](https://ollama.com) installed on the host (not in Docker) and running (`systemctl status ollama`).
- Optional GPU: an NVIDIA GPU with a working driver (`nvidia-smi`). Ollama uses it automatically;
  without one it falls back to CPU (much slower). Verify with `ollama ps` (`PROCESSOR` should say `100% GPU`).
- Docker + Compose, `uv`, Python 3.12+ (for tests/evals).

```bash
ollama pull qwen2.5-coder:3b      # ~1.9 GB, fits a 6 GB GPU with a 16k context
ollama pull qwen2.5-coder:7b      # ~4.7 GB, stronger, see "Choosing a model"
```

## Configuration (`.env`)

```
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5-coder:3b
OLLAMA_BASE_URL=http://localhost:11434   # host-side runs; docker-compose overrides it for containers
OLLAMA_NUM_CTX=16384                      # the context window the model is loaded with
REVIEW_SYNTHESIS=false                    # skip the LLM "second opinion" pass (small models can wrongly drop findings)
WEBHOOK_DRY_RUN=true                      # while testing: webhook-triggered reviews are analysed, never published
EMBEDDING_PROVIDER=hash                   # offline; lexical-only (see README)
```

**Context window.** Ollama truncates prompts that exceed `num_ctx` *silently*. The app therefore budgets
to it: `Settings.context_limit = min(MAX_CONTEXT_TOKENS, (OLLAMA_NUM_CTX - output cap) * 0.7)` and the
provider refuses any prompt that cannot fit (`llm_context_overflow`) instead of sending a truncated one.
`MAX_CONTEXT_TOKENS=24000` is therefore never reached with a 16k window. Larger windows use more VRAM
(KV cache); check `ollama ps` / `nvidia-smi` after changing it.

## Docker → host Ollama networking

The API/worker run in containers; Ollama runs on the host. Containers reach it at
`http://host.docker.internal:11434` (set by `docker-compose.yml`), which resolves to the gateway of a
**pinned** compose bridge (`prbr0`, `172.28.0.0/24`).

1. Ollama must listen beyond loopback so the bridge can reach it:
   ```
   # /etc/systemd/system/ollama.service.d/override.conf
   [Service]
   Environment="OLLAMA_HOST=0.0.0.0:11434"
   ```
   then `sudo systemctl daemon-reload && sudo systemctl restart ollama`.
2. If you use **UFW** (default-deny), allow only the compose bridge to reach Ollama:
   ```bash
   sudo ufw allow in on prbr0 to any port 11434 proto tcp comment 'compose -> host ollama'
   ```
   Do **not** open 11434 to anything else, and never forward it through ngrok: only the API (`:8000`) is
   exposed to GitHub.
3. Verify from the worker:
   ```bash
   docker compose exec -T worker python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:11434/api/version').read())"
   ```

## Startup sequence

```bash
systemctl is-active ollama && ollama list      # Ollama up, model present
docker compose up --build -d
curl localhost:8000/ready                      # database + redis ok
ngrok http 8000                                # only for real GitHub webhooks
```

Set the GitHub App webhook URL to `https://<ngrok-host>/webhooks/github` (the free ngrok URL changes per session).

## Dry run on a real PR (nothing is posted)

```bash
curl -X POST localhost:8000/api/v1/reviews -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"repository":"owner/repo","pull_request":1,"publish":false}'
curl localhost:8000/api/v1/reviews/<id>          -H "X-API-Key: $API_KEY"   # status, scope, token usage
curl localhost:8000/api/v1/reviews/<id>/findings -H "X-API-Key: $API_KEY"   # accepted + rejected with reasons
```
Publish only after inspecting the findings: `POST /api/v1/reviews/<id>/publish`.
Re-reviewing the same head SHA returns the existing job; to force a rerun, push a new commit.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Worker → Ollama times out | Firewall. `journalctl -k \| grep "UFW BLOCK.*DPT=11434"` shows drops on `prbr0`; add the UFW rule above |
| `llm_model_missing` | `ollama pull <LLM_MODEL>` |
| `llm_context_overflow` | Lower `MAX_CONTEXT_TOKENS` or raise `OLLAMA_NUM_CTX` (watch VRAM) |
| Job `FAILED` with connection errors | Ollama stopped or `OLLAMA_BASE_URL` wrong for where the app runs (host vs container) |
| Reviews return no findings | Expected for weak models; see "Choosing a model" and `evals/results/` |
| Slow first request | Model loading into VRAM (`OLLAMA_KEEP_ALIVE` keeps it warm) |

## Choosing a model (measured)

On the 15-PR benchmark (`evals/results/ollama_*.md`): `qwen2.5-coder:3b` reaches 36% precision / 42% recall and
comments on every clean PR; `qwen2.5-coder:7b` reaches 58% / 58% with 0.33 false positives per PR. On a 6 GB GPU
the 7B model runs ~91% on-GPU at `OLLAMA_NUM_CTX=8192` (5.4 GB) and offloads more as the context grows, at ~2x
the latency. **Use 7B for anything you intend to read; keep 3B as a fast baseline.** Neither is a replacement for
a human reviewer or a frontier model: on a real PR both found 1 of ~6 seeded defects.

Settings used: `REVIEW_SYNTHESIS=false` (the 3B model's synthesis pass dropped a valid finding with a made-up
justification), `STATIC_ANALYZERS` left on (no measurable benefit on the benchmark, cheap).
