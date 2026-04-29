# prbe-ai (Claude Code marketplace)

This repo is a Claude Code marketplace that publishes Probe's plugins.
Today it has one plugin: [`prbe-cc-tap-plugin`](./plugins/prbe-cc-tap-plugin/).

## Install (in Claude Code)

```
/plugin marketplace add prbe-ai/prbe-cc-tap-plugin
/plugin install prbe-cc-tap-plugin@prbe-ai
```

Then pair the device with your Probe workspace from a terminal. The `*/`
glob resolves to whichever version Claude Code installed:

```bash
cd ~/.claude/plugins/cache/prbe-ai/prbe-cc-tap-plugin/*/ && \
  python3 -m tap pair <pairing-token>
```

Get a pairing token from your Probe dashboard:
**https://dashboard.prbe.ai → Integrations → Claude Code**.

After pairing, every new Claude Code session will spawn the daemon via the
SessionStart hook and ship transcripts to Probe for ingestion.

## Updates

```
/plugin marketplace update prbe-ai
/plugin install prbe-cc-tap-plugin@prbe-ai   # picks up the new version
```

CC drops new versions in their own subdir, so existing daemons keep running
on the old code until their session ends — no mid-session interruption.

## Repo layout

```
prbe-cc-tap-plugin/                       (this repo — the marketplace)
├── .claude-plugin/marketplace.json       # manifest CC reads
└── plugins/
    └── prbe-cc-tap-plugin/               # the plugin itself
        ├── .claude-plugin/plugin.json
        ├── hooks/
        ├── tap/
        ├── tests/
        ├── pyproject.toml
        └── README.md
```

The plugin's own [README](./plugins/prbe-cc-tap-plugin/README.md) covers the
daemon's design, config files, env vars, and CLI subcommands.

## Migrating from the old curl-installer

If you previously ran the deprecated `curl … | sh` installer, you have plugin
code at `~/.claude/plugins/prbe-cc-tap-plugin/` and a stale entry in
`installed_plugins.json` that CC's `/plugins` UI flags as broken.

To clean up:

1. In CC: `/plugins` → find `prbe-cc-tap-plugin@prbe-ai (user)` → **Remove**.
2. In a terminal, drop the orphaned code but keep the state files (`.token`,
   `state.db`, `logs/`):

   ```bash
   cd ~/.claude/plugins/prbe-cc-tap-plugin
   rm -rf .git .claude-plugin tap hooks scripts tests
   rm -f pyproject.toml uv.lock README.md .gitignore .gitattributes
   ```

3. Install via the new flow (the two slash commands above).

Your `.token` and ingestion state survive, so you don't need to re-pair.

## License

MIT.
