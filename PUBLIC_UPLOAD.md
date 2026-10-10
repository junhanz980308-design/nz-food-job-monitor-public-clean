# Uploading to a NEW public repository

These are instructions for a later, explicitly approved publication. No upload is performed by this document.

1. Use this clean folder only. Do not copy `.git`, secrets, logs, credentials or history from an existing repository. Do not change the existing repository's visibility and do not mirror or fork its private history.
2. On GitHub, create a **new**, empty repository with a different name and Public visibility. Do not initialize it with a README, license or ignore file; this folder already supplies its documentation and ignore rules. Choose a license separately if desired; none is assumed here.
3. In this clean folder, initialize fresh history and stage files:

   ```sh
   git init -b main
   git add .
   git status --short
   git diff --cached --stat
   git diff --cached
   ```

4. Inspect the staged diff. `state/seen.json` must be absent; `state/empty.example.json` must contain only empty collections. Confirm no credential files, personal email addresses, logs or private artifacts are staged. `.gitignore` does not protect files added with `--force`, already tracked files, or secrets pasted into code. Do not use `git add -f`.
5. Configure this repository's commit author with your preferred display name and the exact GitHub-provided no-reply email from your account settings. Do not use a personal email address if you wish to keep it private. Avoid changing global Git configuration unintentionally.
6. Once publication is approved, commit, set the new repository's URL as `origin`, verify `git remote -v` points only to that new repository, and push `main`. Do not push `monitor-state`, use `--mirror`, or reuse an old `.git` directory.
7. Check the published file list and test workflow. Configure the new public deployment with repository Secrets as described in README, including a new MONITOR_STATE_KEY. Monitoring now supports public repositories and publishes only encrypted state. Keep the original private deployment unchanged; running both deployments simultaneously may send duplicate notifications. Do not upload monitoring logs, artifacts or histories to public issues or releases.

Do not upload any existing state branch, even an encrypted one from another deployment. Let the new workflow create its own encrypted monitor-state branch.

The supplied local `state/seen.json` is fresh and ignored. It is not uploaded, and the application recreates it when needed. To share a later code update, repeat the privacy review: this initial review cannot protect files added afterward.

GitHub references: [Import local code](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github) and [commit email privacy](https://docs.github.com/en/account-and-profile/how-tos/email-preferences/setting-your-commit-email-address).
