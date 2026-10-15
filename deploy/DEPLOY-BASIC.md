# Deploying the Macgear reporting site (no SSO)

> **Current name: reporting.macgeargroup.com** — see "Going live as reporting.macgeargroup.com" below.
> The original walkthrough below still says `apps`; the steps are the same.

End state: `https://apps.macgeargroup.com` → Caddy (HTTPS) → the app, which shows its own
styled **/login** page. n8n keeps running untouched. Data (SQLite DB + uploaded files)
lives in `data/` on the droplet.

**Sign-in is per user.** Accounts live in the app's `users` table and are managed at
`/admin/users` by any admin; each person has their own email and password, and each is
granted the tools they need. The old shared `APP_USERNAME` / `APP_PASSWORD` still works
alongside them and signs in as an admin — the migration path and the break-glass way back
in. See "Moving off the shared login" below.

> **Already running the older Caddy `basic_auth` version?** See "Migrating from Caddy
> basic_auth" at the bottom — it's a 3-line change (env vars in, `basic_auth` block out).

> Per-person Microsoft logins are still an option for later: `DEPLOY.md` (the Entra SSO
> version) reuses the same app container and would sit in front of this.

Do the steps in order. You'll collect: a subdomain, the droplet IP, Caddy's docker
network name, and the Caddyfile path inside the Caddy container.

---

## 1. DNS — point a subdomain at the droplet

In your DNS provider add an **A record**:

| Type | Name      | Value (points to)   |
|------|-----------|---------------------|
| A    | `apps`    | `170.64.200.224`    |

(Full host becomes `apps.macgeargroup.com`.) Give it a few minutes, then verify:

```bash
ping apps.macgeargroup.com      # should show 170.64.200.224
```

Caddy can't get an HTTPS certificate until this resolves to the droplet.

---

## 2. Get the code onto the droplet

SSH in (`ssh root@170.64.200.224`), then clone from GitHub:

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/MacgearCode/Vendor-credit-claims.git vendor-credit-claims
cd vendor-credit-claims
```

(If the repo is private, git will prompt for your GitHub username + a personal access
token as the password. Or make a deploy key — but a one-off token paste is fine.)

---

## 3. Find Caddy's container name and docker network

The app container must join the same docker network Caddy is on so Caddy can reach it.

```bash
docker ps --format '{{.ID}}  {{.Names}}  {{.Image}}' | grep -i caddy
CADDY=$(docker ps --filter ancestor=caddy -q | head -1)
docker inspect $CADDY -f '{{.Name}} -> {{.Config.Image}}'        # sanity-check: ONE container
docker inspect $CADDY -f '{{json .NetworkSettings.Networks}}'    # network name(s)
docker inspect $CADDY -f '{{json .Mounts}}'                      # where the Caddyfile lives
```

Filter on **`ancestor=caddy`** (the image), not `name=caddy`. The n8n compose project is
called `n8n-docker-caddy`, so a name filter matches the n8n container too, `$CADDY` ends
up holding two ids, and every `docker exec $CADDY …` below fails.

On this droplet the answers are:

- Caddy container: `n8n-docker-caddy-caddy-1`
- Network: `n8n-docker-caddy_default` — goes in `.env` as `CADDY_NETWORK`
- Caddyfile on the host: `/opt/n8n-docker-caddy/caddy_config/Caddyfile`, mounted at
  `/etc/caddy/Caddyfile` inside the container — that's the path you edit in step 6.

---

## 4. Configure and start the app

```bash
cd /opt/vendor-credit-claims/deploy
cp .env.basic.example .env
nano .env            # CADDY_NETWORK, APP_USERNAME, APP_PASSWORD, APP_SECRET,
                     # PUBLIC_BASE_URL — see step 5

docker compose -f docker-compose.basic.yml up -d --build
docker compose -f docker-compose.basic.yml logs -f    # watch it start; Ctrl-C to stop watching
```

On first start the app builds its product/retailer seed from the template, creates the
database, and starts serving on port 8000 inside the network (not exposed to the internet).

---

## 5. Set the auth environment

Auth lives in the app — no bcrypt hashing, no Caddy directive. In `deploy/.env` set:

```
APP_USERNAME=macgear                  # the shared fallback login
APP_PASSWORD=ChooseAStrongSharedPassword
APP_SECRET=<paste output of: openssl rand -hex 32>
PUBLIC_BASE_URL=https://apps.macgeargroup.com
```

- `APP_SECRET` signs the login cookie. Use a long random string and keep it stable —
  changing it just signs everyone out (no data impact).
- `PUBLIC_BASE_URL` is what set-password links are built from. Leave it out and the app
  falls back to the request's own base URL, which behind Caddy is the container's internal
  address — links nobody outside the docker network can open.
- **`APP_PASSWORD` blank *and* no accounts yet = auth disabled entirely.** That is the
  local-dev shortcut and must never be a deployment's state. Once real accounts exist you
  can blank it safely; see "Moving off the shared login".

After editing `.env`, apply it:

```bash
docker compose -f docker-compose.basic.yml up -d
```

---

## 6. Add the subdomain to Caddy

Edit the Caddyfile from step 3 and append (see `Caddyfile.basic.snippet`) — note there's
**no `basic_auth` block**, the app handles login itself:

```
apps.macgeargroup.com {
    reverse_proxy claims:8000
}
```

Reload Caddy (no downtime). Use the Caddyfile path **inside** the container
(usually `/etc/caddy/Caddyfile`):

```bash
docker exec $CADDY caddy reload --config /etc/caddy/Caddyfile
```

Caddy fetches a Let's Encrypt certificate for the subdomain automatically.

---

## 7. Test

Open `https://apps.macgeargroup.com` → the app's **/login** page appears → enter the
`APP_USERNAME` / `APP_PASSWORD` from `.env` → you land on the **tool picker**. From there:

- **Vendor Promos** → `/promos`, the promotions dashboard.
- **PDF Bill Upload** → `/vendor-bills`. Upload one of last month's Amazon Advertising
  PDFs and check the review table matches the invoice before telling accounts it's live.

"Sign out" is in the top nav.

Don't hand the shared credentials out — go straight to "Moving off the shared login"
below and give each person their own account instead.

---

## Day-2 operations

**Update the app after code changes** (pull, then rebuild just the app):
```bash
cd /opt/vendor-credit-claims && git pull
cd deploy && docker compose -f docker-compose.basic.yml up -d --build
```

**Add or remove a person, or change what they can reach:** `/admin/users` in the app.
No deploy, no restart, no env change.

**Someone forgot their password:** open their account at `/admin/users`, hit
*Generate set-password link*, send it to them. It's single use and lasts 24 hours,
and it kills any earlier link for that account. You never see their password.

**Change the shared fallback password:** edit `APP_USERNAME` / `APP_PASSWORD` in
`deploy/.env`, then `docker compose -f docker-compose.basic.yml up -d`. No Caddy change,
no rebuild. (Existing sessions stay valid until they expire or you rotate `APP_SECRET`.)

**Rotate `APP_SECRET`:** same procedure. Everyone is signed out; no data impact.

**Back up the data** (DB + uploaded files) — the important one:
```bash
tar czf claims-backup-$(date +%F).tar.gz -C /opt/vendor-credit-claims data
```
Copy that off the droplet regularly (or fold it into your n8n backups).

**Update product master / rebates:** replace `AU_Promo Form_TEMPLATE.xlsx` in the repo,
push, `git pull` on the droplet, then rebuild (existing promos are untouched; reference
data is upserted).

**Logs:** `docker compose -f docker-compose.basic.yml logs -f`

---

## Troubleshooting

- **502 from Caddy:** the app isn't reachable on Caddy's network — confirm `CADDY_NETWORK`
  in `.env` matches step 3, and `docker compose -f docker-compose.basic.yml ps` shows the
  `claims` container up.
- **No HTTPS cert:** DNS isn't pointing at the droplet yet, or ports 80/443 aren't open.
- **/login rejects the shared password:** check `APP_USERNAME` / `APP_PASSWORD` in
  `deploy/.env`, then `docker compose -f docker-compose.basic.yml up -d` to reload them
  (env changes need the container recreated).
- **/login rejects a real account:** the account may be inactive, or have no password set
  yet — `/admin/users` shows "no password yet" against it. Generate a fresh link.
  The error message is deliberately the same either way, so it never reveals whether an
  email has an account.
- **A set-password link 404s or points at `claims:8000`:** `PUBLIC_BASE_URL` isn't set.
  Add it to `deploy/.env`, recreate the container, then generate a *new* link — the old
  one has the wrong host baked in.
- **"Link expired":** the link was already used, is over 24 hours old, or a newer link was
  generated for that account. Generate another.
- **No sign-in page at all:** `APP_PASSWORD` is blank *and* the `users` table is empty, so
  the app has disabled auth. Set one or create an account immediately.
- **Browser still pops a basic-auth box:** you've still got the old `basic_auth` block in
  the Caddyfile — remove it (see the migration note below) and reload Caddy.
- **Everyone got logged out:** `APP_SECRET` changed (or wasn't set, so it defaults per
  build). Set a fixed `APP_SECRET` in `.env` and recreate the container.
- **Locked out entirely:** put `APP_PASSWORD` back in `deploy/.env`, recreate the
  container, sign in with the shared credentials and fix the accounts. The app won't let
  you demote, disable or delete the last active admin, so this needs the row to have been
  removed outside the app.

---

## Moving off the shared login to per-user accounts

The app has real accounts now, managed at **/admin/users** by any admin. The old
shared `APP_USERNAME` / `APP_PASSWORD` still works *alongside* them and signs you
in as an admin — that is deliberate, so a bad rollout can't lock everyone out.

Do it in this order:

1. **Set `PUBLIC_BASE_URL`** in `deploy/.env` to `https://apps.macgeargroup.com`,
   then recreate the container. Without it, set-password links are built from the
   app's internal address (`http://claims:8000`) and nobody can open them.
   ```bash
   cd /opt/vendor-credit-claims/deploy && docker compose -f docker-compose.basic.yml up -d
   ```
2. **Sign in with the shared login** and create your own admin account at
   `/admin/users/new`. Copy the single-use link it shows you.
3. **Open the link, set your password**, then sign out and sign back in *as
   yourself*. Confirm you can reach `/admin/users`. Do not skip this.
4. **Create everyone else**, ticking only the tools each person needs. Send each
   person their own link — one at a time, not in a group chat; whoever holds a
   link can set that password.
5. **Blank `APP_PASSWORD`** in `deploy/.env` and recreate the container. The
   shared login stops working; per-user accounts continue.
   ```bash
   docker compose -f docker-compose.basic.yml up -d
   ```

**Never blank `APP_PASSWORD` while the users table is empty** — the app treats
"no shared password and no accounts" as local dev and disables auth completely.

**If you lose the last admin account**, put `APP_PASSWORD` back in `deploy/.env`,
recreate the container, and sign in with the shared credentials to fix it. The
app refuses to demote, disable or delete the only remaining active admin, so this
should only happen if the row is removed outside the app.

Links expire after 24 hours and are single use. Generating a new one for someone
kills their previous link. Passwords are stored as PBKDF2-SHA256 hashes; an admin
never sees or sets another person's password.

---

## Account emails — invites and "Forgot password?" (2026-10)

The site sends invites and password resets through the **3PL Portal's n8n workflow**
(`3PL Portal/n8n/3pl_account_email.json` → Graph app-only → `noreply@macgeargroup.com`).
Nothing new in Microsoft 365 or n8n — only two lines in this app's `deploy/.env`:

```bash
# copy both lines from the 3PL Portal's env (it lives at /opt/threepl/3PL-Portal/.env);
# values aren't printed — the last command shows only the names, each should appear once
cd /opt/vendor-credit-claims/deploy
grep -E '^(N8N_RESET_WEBHOOK_URL|N8N_WEBHOOK_TOKEN)=' /opt/threepl/3PL-Portal/.env >> .env
grep -oE '^(N8N_RESET_WEBHOOK_URL|N8N_WEBHOOK_TOKEN)=' .env
docker compose -f docker-compose.basic.yml up -d
```
(Done 2026-10-15.)

Test: `/admin/users` → a user → **Email password reset** to yourself. The notice should say
"emailed"; the email arrives from noreply@ within a few minutes (**check Junk** — a new sender
reputation is normal, see 3PL `docs/email_delivery.md` "Triage"). In n8n → Executions the
run shows `app: reporting`.

Without the two lines the site still works: admins get "Email isn't set up on this server"
and a link to copy, and "Forgot password?" shows the usual confirmation but sends nothing.

---

## Going live as reporting.macgeargroup.com (apps → reporting, 2026-10)

The site became the reporting hub: reports over the warehouse, the tools under a
Tools menu, vendor accounts. Same repo, same container (`claims`), same `data/` —
only the public name changes, and the app now also reads the reporting warehouse.
n8n's pricing sync (`http://claims:8000`) is unaffected.

**0. Back up first** (on the droplet):
```bash
cd /opt/vendor-credit-claims
tar czf ~/claims-backup-$(date +%F-%H%M).tar.gz data
```

**1. DNS:** add an **A record** `reporting` → `170.64.200.224`. Keep the `apps` and
`promos` records — they answer the redirects.
```bash
dig +short reporting.macgeargroup.com     # must return 170.64.200.224 before step 5
```

**2. Let the droplet reach the warehouse:** DigitalOcean → Databases → the reporting
cluster → Settings → **Trusted sources** → add this droplet (the n8n droplet,
170.64.200.224). Without it every report says "the warehouse could not be reached"
(the tools still work).

**3. Pull the code and set the new env** (on the droplet):
```bash
cd /opt/vendor-credit-claims && git pull
cd deploy && nano .env
```
In `.env`:
- `REPORTING_DB_URL=` — the **read-only** `reporting_read` URL (same value as
  `REPORTING_READ_DB_URL` in `Sell Through/.env`). Paste it on the droplet only;
  never commit it or send it in chat.
- `PUBLIC_BASE_URL=https://reporting.macgeargroup.com`
- `APP_SECRET=` must be a long random value (`openssl rand -hex 32` if it is blank).
  **The app now refuses to start** when `PUBLIC_BASE_URL` is set but `APP_SECRET`
  isn't, or when there is neither a shared password nor any user account.
```bash
grep -c '^APP_SECRET=..' .env        # 1 = set, 0 = blank
```

**4. Rebuild and start:**
```bash
docker compose -f docker-compose.basic.yml up -d --build
docker compose -f docker-compose.basic.yml logs --tail 40
```
Look for `Application startup complete`. `Refusing to start: …` means step 3 isn't
done. The first report views after a restart may take a few seconds while the cache
warms; after that they're near-instant.

**5. Caddy:** replace the `apps.macgeargroup.com { reverse_proxy claims:8000 }` block
and the old `promos` redirect with the three blocks in `Caddyfile.basic.snippet`
(reporting = the site; apps and promos = permanent redirects to it), then:
```bash
CADDY=$(docker ps --filter ancestor=caddy -q | head -1)
docker exec $CADDY caddy reload --config /etc/caddy/Caddyfile
```

**6. Verify:**
```bash
curl -sI https://reporting.macgeargroup.com/login | head -1        # 200
curl -sI https://apps.macgeargroup.com/promos | grep -i location     # -> https://reporting.../promos
curl -sI https://promos.macgeargroup.com/ | grep -i location         # -> https://reporting.../
```
Then sign in: Home shows AU/NZ sales (admins), the receipt strip, and the reports.

**7. Give people their reports:** existing staff accounts were created when the site
only had tools, so they see **no reports until ticked** (default-deny). In
`/admin/users` tick each person's reports, and "Sell-in $" / "Margin" only for those
who should see them.

**Do not create real vendor accounts yet** — first the database-level isolation (RLS
role), two-factor sign-in, an access log, and blanking `APP_PASSWORD` (see the
project `CLAUDE.md`, "Before the first real vendor account").

**Roll back:** `git checkout <previous commit>` in `/opt/vendor-credit-claims`, rebuild
(step 4), and put the old `apps` block back in Caddy. The database changes are
additive (three new user columns), so the old code runs on the same `data/`.

---

## Renaming the subdomain (promos → apps, done 2026-08-05)

The app outgrew `promos.macgeargroup.com` when it gained a second tool. Steps, in
this order — the redirect can't be added until the new name has a certificate:

1. **DNS:** add an **A record** `apps` → `170.64.200.224`. Leave the `promos`
   record in place; it's what the redirect answers on.
   ```bash
   dig +short apps.macgeargroup.com     # must return 170.64.200.224 before step 3
   ```
2. **Deploy the new code** (the two-tool version) so the site being pointed at is
   the one people are meant to land on:
   ```bash
   cd /opt/vendor-credit-claims && git pull
   cd deploy && docker compose -f docker-compose.basic.yml up -d --build
   ```
3. **Caddy:** replace the old `promos.macgeargroup.com { reverse_proxy claims:8000 }`
   block with **both** blocks from `Caddyfile.basic.snippet` — the new site and the
   redirect — then reload:
   ```bash
   docker exec $CADDY caddy reload --config /etc/caddy/Caddyfile
   ```
4. **Verify both:**
   ```bash
   curl -sI https://apps.macgeargroup.com/login | head -1        # 200
   curl -sI https://promos.macgeargroup.com/promos | head -2     # 301 -> apps...
   ```

Nothing else needs touching. The docker service is still `claims`, so **n8n's
pricing sync keeps working unchanged** — it posts to `http://claims:8000` on the
shared docker network and never resolves the public hostname.

Tell sales and finance the new URL, but the old one keeps working: the redirect
preserves the path, so bookmarks to a specific promo still land correctly.

---

## Migrating from Caddy basic_auth (existing deploy)

If you deployed the earlier version where Caddy popped the browser login box:

1. **Add the login env** to `deploy/.env` (reuse your existing shared password if you like):
   ```
   APP_USERNAME=macgear
   APP_PASSWORD=YourSharedPassword
   APP_SECRET=<openssl rand -hex 32>
   ```
2. **Pull + recreate the app:**
   ```bash
   cd /opt/vendor-credit-claims && git pull
   cd deploy && docker compose -f docker-compose.basic.yml up -d --build
   ```
3. **Remove the `basic_auth { … }` block** from the Caddyfile (leave just
   `reverse_proxy claims:8000` inside the site block), then reload Caddy:
   ```bash
   docker exec $CADDY caddy reload --config /etc/caddy/Caddyfile
   ```

Now the browser popup is gone and you get the styled `/login` page instead. If you skip
step 3 you'll briefly get *both* the popup and the page.
