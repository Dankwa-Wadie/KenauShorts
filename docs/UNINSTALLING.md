# Uninstalling / Resetting KenauShorts

Everything KenauShorts writes stays inside its own project folder or your home directory's service-manager config — nothing is installed system-wide. What you remove depends on what you're trying to do.

---

## Option A: Reset (keep KenauShorts, start fresh)

Use this if something's misconfigured and you'd rather start clean than debug it. This deletes generated data and local state but keeps your code, `.venv`, and API keys.

1. Stop the app if it's running (close the terminal running `scripts/start.py`, or see "Remove the background service" below if you installed it as a service).
2. Delete the generated/runtime files from the project root:
   ```bash
   # macOS / Linux
   rm -rf work/ out/ logs/ studio.sqlite3 state.json .pipeline.lock .studio.lock

   # Windows (PowerShell)
   Remove-Item -Recurse -Force work, out, logs, studio.sqlite3, state.json, .pipeline.lock, .studio.lock -ErrorAction SilentlyContinue
   ```
3. Optionally also delete `config.json` and `studio-settings.json` if you want the First-Run Setup Wizard to run again from scratch (your API keys in `studio-secrets.json` and your YouTube `token.json` are separate files and won't be touched by this).
4. Run `python3 scripts/start.py` again.

---

## Option B: Remove the background service, keep everything else

If you ran `scripts/install_service.py`, KenauShorts starts automatically on login/boot. To stop that without touching anything else:

```bash
python3 scripts/uninstall_service.py
```

This removes the launchd agent (macOS), Task Scheduler task (Windows), or systemd user unit (Linux) that `install_service.py` created. It's safe to run even if you never installed the service — it just reports there's nothing to do.

---

## Option C: Permanently uninstall

Deleting the project folder is most of it, but a few things live outside it — mainly the credentials you granted KenauShorts, which stay valid on Google's/the provider's side until you revoke them there. Do this if you're done with the project for good.

### 1. Remove the background service (if installed)

```bash
python3 scripts/uninstall_service.py
```

### 2. Revoke YouTube upload access

Deleting `token.json` locally does **not** revoke the grant on Google's side — the OAuth consent stays valid until you remove it yourself:

1. Go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
2. Find the OAuth client you created for KenauShorts (named whatever you called it in Google Cloud Console, e.g. "KenauShorts Desktop Client").
3. Click it → **Remove Access**.

If you also want to delete the OAuth client itself (not required, but tidy), go to [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials) and delete the Desktop client, or delete the whole Google Cloud project you created for it.

### 3. Delete your AI provider API keys

Keys pasted into the Connections tab live in `studio-secrets.json` in plain text (locally only — see the Privacy & Security section in the main README). Deleting the file removes them from your machine, but the keys themselves remain active on the provider's side until revoked there:

- **Gemini**: [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) → delete the key.
- **Anthropic**: [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) → delete the key.
- **OpenAI**: [platform.openai.com/api-keys](https://platform.openai.com/api-keys) → delete the key.
- **Reddit** (if you configured `REDDIT_CLIENT_ID`/`SECRET`): [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) → delete the app.

### 4. Remove the Python environment and project folder

```bash
# macOS / Linux
deactivate 2>/dev/null  # if the venv is active
cd ..
rm -rf KenauShorts

# Windows (PowerShell)
deactivate 2>$null
cd ..
Remove-Item -Recurse -Force KenauShorts
```

That's everything — the venv, all generated videos, all local config and credentials, and the code itself are gone. Steps 2 and 3 are the ones people usually forget, since nothing on your own machine will remind you those grants still exist.
