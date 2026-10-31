# job-watcher

Checks company job boards straight at the source (Greenhouse, Lever, Ashby, Workday, Workable, BambooHR,
Rippling, Oracle, Jobvite, JazzHR, Teamtailor, Pinpoint, Jibe, SmartRecruiters, Recruitee, Breezy) every
30 minutes. You get an email with **company, role, location, when the ATS says it was posted, and the
apply link** as soon as a new posting appears.

## How "new" is decided (accuracy rules)
- **Identity = the ATS's own job ID**, never title or date. A job is new only if that ID has never been
  seen for that company.
- **First fetch of a board is a silent baseline.** Roles that are already open are recorded but not
  emailed, so adding a company never floods you. The exception is roles the ATS says were published in
  the last 24 h, which are still sent.
- **Disappear and come back:** within 72 h it's restored silently (an ATS glitch). After that it's
  emailed as *reopened*.
- **New ID, same title and location as a recently closed role:** emailed and tagged *reposted*.
- **Outages never cause false alerts:** a failed fetch, an empty board, a partial page set or a sudden
  >50 % shrink leaves saved state untouched. A board failing for 3 h triggers one warning email, and you
  get a "back online" note when it recovers.
- **State is saved only after the email sends.** If Gmail fails, the same jobs are retried on the next run.
- Big boards (NVIDIA Workday, AMD and Rivian Jibe) are filtered to Canada **server-side**. Everything
  else is filtered by title (engineering and data roles; interns, managers and sales excluded) and by
  location (Canada, a Canadian city, or remote with no other region named). Both filters live in
  `config.toml`.
- "Posted" in the email is the ATS's own publish time when it exposes one. Workday shows only
  "Posted Today", so the script reads the exact date from the job's detail page. Detection time is in
  the email header.

## Option A: GitHub Actions (runs 24/7, recommended)
1. **Gmail app password:** turn on 2-Step Verification, then go to <https://myaccount.google.com/apppasswords>,
   create one named `job-watcher` and copy the 16 characters.
2. **Create a repo** on github.com, e.g. `job-watcher`. Choose **Public** for unlimited free Actions
   minutes; your email and password stay in encrypted secrets. Private also works and uses about 1,500 of
   the 2,000 free minutes a month.
3. Upload this folder to the repo. Include `.github/workflows/job-watch.yml`; the `.github` folder is
   hidden in Finder, so press ⌘⇧. to see it, or use git:
   `git init && git add . && git commit -m init && git branch -M main && git remote add origin https://github.com/<you>/job-watcher.git && git push -u origin main`
4. Go to Repo → **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `SMTP_USER`: your Gmail address
   - `SMTP_PASSWORD`: the app password
   - `ALERT_TO`: `mall.shubhkumar@gmail.com`
5. Go to Repo → **Actions → job-watch → Run workflow** and run, in order:
   1. `validate`: the log lists every board with its job count
   2. `test-email`: you should get a sample alert
   3. `run`: the baseline, which sends one "Now tracking" email

   From then on it runs by itself every 30 min.

Note: GitHub can start scheduled runs 5–15 min late at busy times. The script commits a heartbeat at
least weekly, so GitHub never pauses the schedule for inactivity.

## Option B: your Mac (launchd)
```bash
cd job-watcher
bash mac/install.sh        # asks for Gmail + app password once, stored in ~/.config/job-watcher/env (chmod 600)
```
It runs every 30 min **while the Mac is awake**; a run missed during sleep fires on wake. The log is at
`logs/watcher.log`. Remove it with `bash mac/install.sh uninstall`. Don't run A and B at the same time,
or you'll get duplicate emails.

## Everyday use
- **Add a company:** `python3 discover.py "Company Name"`, or pass slugs to try:
  `python3 discover.py "Acme" acme acmeinc`. Then paste the printed block into `config.toml`.
- **Test locally without sending or saving:** `python3 watcher.py --dry-run`, or for one company
  `python3 watcher.py --mode validate --only "Cohere"`.
- **Per-company options:**
  - `titles = "all"`: alert on every role
  - `all_locations = true`: skip the location filter
  - `enabled = false`: pause the company
- **History:** every alert ever sent is appended to `state/history.jsonl`.
- **Tests:** `python3 -m unittest discover -s tests -v`
