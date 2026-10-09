# Host the hooks server (Render, free tier) — ≈ 45 min

Why: Sarvam's cloud can only call a public URL that stays up while judges test. The server uses a **small demo database**
(only the ~22 GLIDs in `samples/` and `examples/demo_campaign_contacts.csv`), not the full dataset.

## 1. Build the demo database (on your laptop, project folder, venv active)
    python scripts/make_demo_db.py
Expect: `wrote deploy/demo.sqlite (… KB): N events, ~20 GLIDs`. (Needs `var/gcx.sqlite` from `python -m gcx ingest`.)
Open `deploy/demo.sqlite` size: should be well under 5 MB. Add more GLIDs with `--extra 123,456`.

## 2. Put the project on GitHub (private repo is fine)
    git init && git add . && git commit -m "global context"
Check `git status` first: **`.env`, `data/`, `var/` must NOT be included** (they are in `.gitignore`).
`deploy/demo.sqlite` must be included. Create an empty GitHub repo, then `git remote add origin <url> && git push -u origin main`.

## 3. Deploy on Render
1. render.com → sign up → **New → Web Service** → connect the GitHub repo.
2. Runtime **Docker** (it finds the `Dockerfile`), plan **Free**. (`render.yaml` has the same settings.)
3. **Environment**: add `GCX_HOOK_TOKEN` = a long random string (e.g. `python -c "import secrets;print(secrets.token_urlsafe(32))"`).
   Optional: `SARVAM_API_KEY` (new key — the old one was exposed) for chat/WhatsApp replies.
4. Deploy. When it is live you get `https://<name>.onrender.com`. Check: open `https://<name>.onrender.com/health` → `{"ok":true,...}`.
5. The free tier sleeps after ~15 min idle (first request then takes 30–60 s). Open `/health` shortly before judging.

## 4. Test the hooks from your terminal
    curl -s -X POST https://<name>.onrender.com/hooks/on-start \
      -H "Authorization: Bearer <GCX_HOOK_TOKEN>" -H "Content-Type: application/json" \
      -d '{"glid":"273498704"}'
You should get JSON with `opening_line`, `open_items`, …

## 5. Point Sarvam at it
In the agent: On-start hook → `POST https://<name>.onrender.com/hooks/on-start`, body `{"glid":"@glid"}`, header `Authorization: Bearer <token>`;
map `opening_line, language, open_items, context_md` into the variables. On-end hook → `POST …/hooks/on-end` with
`glid, call_id, disposition, summary, transcript`. (If the hook screen wants another shape: `find_glid` / `as_vars` in `gcx/server.py`.)

## Notes
- Render's disk is ephemeral: live events written during judging survive until the service restarts, then the demo DB resets. That is fine for a demo.
- Never put real data in a public repo. The demo DB still contains real company names from the export; keep the repo **private**
  and confirm with the organisers that hosting these ~20 records is allowed.
