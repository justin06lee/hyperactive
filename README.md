# hyperactive (repo)

A Hermes Agent plugin: **Hermes, but it texts you first.** The plugin itself, and its user README,
live in [`plugin/`](plugin/README.md). That directory is what the catalog installs
(`subdir: plugin`).

```
plugin/     the plugin: __init__.py (tools, /ha, hooks), store.py (SQLite), pulse.py (cron script)
tests/      pytest: store + pulse (stdlib only) and register() against a real Hermes
dev/        stub_llm.py (scripted fake model, no API key), smoke.sh, demo.sh, media.py (catalog images)
docs/media/ catalog image (2:1) and screenshots, rendered by dev/media.py from dev/media.json
catalog/    catalog entry for NousResearch/hermes-agent plugin-catalog/, pin.sh, PR.md (draft PR text)
DESIGN.md   design and the 3 demo moments; NEXT.md: what's left
```

Development uses an isolated scratch Hermes, never `~/.hermes`. Setup is described under "Dev setup"
in DESIGN.md.

```bash
. .scratch/env.sh
python -m pytest -q tests
hermes plugins validate plugin --install-deps
dev/demo.sh          # the 3 demo moments with 1-minute timings against the stub model
# or against a real OpenAI-compatible model (key in the scratch .env, never here):
DEMO_MODEL_URL=https://host/v1 DEMO_MODEL=name DEMO_MODEL_KEY_ENV=VAR dev/demo.sh
```

Repo: https://github.com/justin06lee/hyperactive. MIT licensed, by Justin.
