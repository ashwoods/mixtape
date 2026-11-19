---
name: ci-after-landing
description: Push a mixtape landing to GitHub and confirm its CI (Ubuntu rs, Ubuntu stock, macOS, docs) with scripts/ci-status, polling without blocking. Use after a branch lands on main in ~/Code/mixtape, and whenever someone asks whether CI passed for a commit.
---

# CI after landing

The lander checks a landing locally; GitHub Actions then runs CI on it. CI has three jobs (`ci.yml`: Ubuntu 24.04 with
gst-plugins-rs, stock Ubuntu, macOS with Homebrew) plus the docs build (`docs.yml`). A landing is done once all four
have passed.

1. Push from the user's checkout, never forced: `git -C ~/Code/mixtape push origin main`. Skip this when the lander
   pushes itself.
2. Poll in one `bash` call with `interruptible: true`, so a steering message ends it early, and `timeout: 1500`.
   Keep each sleep at 30 s or less, and never run the push in the same call:

   ```bash
   cd ~/Code/mixtape && sha=$(git rev-parse main)
   until scripts/ci-status "$sha"; [[ $? != 8 ]]; do sleep 30; done
   ```

   `scripts/ci-status [COMMIT]` prints one line per job: ✓, ✗ or …, the job, its state, its time and its URL. It
   returns at once: exit 0 means every job passed, 1 means one failed or was cancelled, and 8 means one is still
   queued or running, or no run has started yet. Never use `gh run watch`: it blocks without output. The macOS job
   takes about 6 minutes. When the call times out after 25 minutes, run `scripts/ci-status` once more and report what
   is still running.
3. Report the final `scripts/ci-status` lines. On ✗, open the job's URL with `gh run view --log-failed --job <id>`
   (the id is the last number of the URL) and name the failing test. A timing bound that broke is a flaky XFAIL,
   never a failure (CONTRIBUTING, "Tests that may be flaky"). A real failure goes back to a branch to fix; never
   re-run a job until it passes.
