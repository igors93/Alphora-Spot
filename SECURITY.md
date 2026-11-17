# Security Policy

## Sensitive Data Warning

Alphora connects to Binance Futures and handles API keys and trading credentials. Please read this section carefully before deploying in any live environment.

**Never commit the following to version control:**

- `.env` files containing API keys or secrets
- `config/*.local.json` files (they are git-ignored for this reason)
- Any file containing `api_key`, `api_secret`, or exchange credentials

---

## Supported Versions

| Version | Status |
|---|---|
| `main` branch | Actively maintained |
| Older snapshots | Not supported |

---

## Reporting a Vulnerability

If you discover a security vulnerability — especially one that could expose API credentials, bypass execution-mode guards, or cause unintended real orders — please **do not open a public GitHub issue**.

Instead, report it privately:

1. Email the maintainer directly (check the git commit history for contact details).
2. Include a clear description of the vulnerability, steps to reproduce, and potential impact.
3. Allow reasonable time for a fix before any public disclosure.

We aim to acknowledge reports within 48 hours and release a patch within 7 days for critical issues.

---

## Execution Mode Guards

Alphora has built-in safety guards to prevent accidental live trading:

- `TESTNET_LIVE` and `REAL_LIVE` modes **block direct `python main.py` runs** unless `ALPHORA_ALLOW_CLI_RUN=true` is explicitly set.
- `mutations_enabled` must be `true` in config for any real order to be sent.
- The GUI sets `ALPHORA_LAUNCHED_BY_GUI=true` automatically — the CLI flag is for intentional headless use only.

Do not remove or weaken these guards in contributions.

---

## API Key Permissions

When creating Binance API keys for use with Alphora:

- Enable **Futures trading** only.
- **Disable withdrawals** entirely.
- Restrict API access to your server's IP address if possible.
- Rotate keys regularly, especially after any suspected exposure.

---

## Dependency Security

Dependencies are pinned in `pyproject.toml`. Before upgrading any dependency, review its changelog for security advisories. Run `pip install -e ".[dev]"` after changes to `pyproject.toml` to ensure your environment matches.
