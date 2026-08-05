# Deploying Vendor Credit Claims (DigitalOcean droplet, behind your n8n Caddy, Entra SSO)

End state: `https://apps.macgeargroup.com` → Caddy (HTTPS) → oauth2-proxy (Microsoft
Entra login) → the app. Only people in your M365 tenant with your email domain can get in.
n8n keeps running untouched. Data (SQLite DB + uploaded files) lives in `data/` on the droplet.

Do the steps in order. Things you'll collect along the way: subdomain, droplet IP,
tenant ID, client ID, client secret, cookie secret, Caddy's docker network name,
Caddyfile path.

---

## 1. DNS — point a subdomain at the droplet

In your DNS provider, add an **A record**:

| Type | Name              | Value (points to)        |
|------|-------------------|--------------------------|
| A    | `claims`          | `<your droplet IP>`      |

(Full host becomes `apps.macgeargroup.com`.) Give it a few minutes to propagate;
verify with `ping apps.macgeargroup.com` showing the droplet IP.

---

## 2. Register the app in Microsoft Entra (Azure AD)

Portal: https://portal.azure.com → **Microsoft Entra ID → App registrations → New registration**.

1. **Name:** `Vendor Credit Claims (SSO)`
2. **Supported account types:** *Accounts in this organizational directory only* (single tenant).
3. **Redirect URI:** platform **Web**, value:
   `https://apps.macgeargroup.com/oauth2/callback`
4. Click **Register**, then copy:
   - **Application (client) ID**  → `OAUTH2_PROXY_CLIENT_ID`
   - **Directory (tenant) ID**    → `ENTRA_TENANT_ID`
5. **Certificates & secrets → New client secret** → copy the **Value** immediately
   (you can't see it again) → `OAUTH2_PROXY_CLIENT_SECRET`.
6. (Recommended) Restrict who can sign in: **Enterprise applications →** open this app **→
   Properties →** set **Assignment required = Yes →** then **Users and groups →** add the
   staff who should have access. Combined with the email-domain check, only assigned
   colleagues get in.

> This is a *separate* registration from your existing "Macgear Claude Agent" Graph app —
> keep them distinct; this one is just for web sign-in.

---

## 3. Get the code onto the droplet

SSH in (`ssh root@<droplet IP>`), then:

```bash
# Docker is already installed by the n8n one-click. Put the app somewhere sensible:
mkdir -p /opt && cd /opt
# Option A: copy from your machine with scp/rsync, or
# Option B: clone if you've pushed it to git, e.g.:
#   git clone <your-repo-url> vendor-credit-claims
cd vendor-credit-claims
```

If copying from your Windows machine instead, from your local project folder:

```powershell
scp -r "C:\Users\aaron\Desktop\Claude Projects\Vendor Credit Claims" root@<droplet IP>:/opt/vendor-credit-claims
```

(Exclude the local `.venv` — it's not needed on the server.)

---

## 4. Find Caddy's docker network and Caddyfile

The app must join the same docker network Caddy uses, and you'll add a site to its Caddyfile.

```bash
docker ps                       # find the caddy container name (e.g. "caddy")
docker network ls               # list networks; note the one caddy is attached to
docker inspect caddy -f '{{json .NetworkSettings.Networks}}'   # shows caddy's network name
docker inspect caddy -f '{{json .Mounts}}'                     # shows where the Caddyfile is mounted from
```

- The **network name** (e.g. `root_default`, `n8n_default`, or similar) goes into
  `CADDY_NETWORK` in your `.env`.
- The **Caddyfile path** on the host (from the Mounts output) is the file you'll edit in step 6.

---

## 5. Configure and start the app + SSO gate

```bash
cd /opt/vendor-credit-claims/deploy
cp .env.example .env
nano .env        # fill in every value

# generate the cookie secret and paste it into .env as OAUTH2_PROXY_COOKIE_SECRET:
openssl rand -base64 32

# build and start (app + oauth2-proxy)
docker compose up -d --build
docker compose logs -f          # watch it start; Ctrl-C to stop watching
```

The app seeds its product/retailer reference data and creates the database on first start.

---

## 6. Add the subdomain to Caddy

Edit the Caddyfile found in step 4 and append (see `Caddyfile.snippet`):

```
apps.macgeargroup.com {
    reverse_proxy oauth2-proxy:4180
}
```

Reload Caddy (no downtime):

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
# (if your Caddyfile is at a different path inside the container, use that path)
```

Caddy will fetch a Let's Encrypt certificate for the subdomain automatically.

---

## 7. Test

Open `https://apps.macgeargroup.com` → you should be redirected to a Microsoft login →
after signing in with a tenant account on your domain, you land on the dashboard.
Try with a non-allowed account to confirm it's blocked.

---

## Day-2 operations

**Update the app after code changes** (re-copy/pull the code, then):
```bash
cd /opt/vendor-credit-claims/deploy
docker compose up -d --build app
```

**Back up the data** (DB + uploaded files) — this is the important one:
```bash
tar czf claims-backup-$(date +%F).tar.gz -C /opt/vendor-credit-claims data
```
Copy that file off the droplet regularly (or add it to whatever backs up your n8n data).

**Update product master / rebates:** replace `AU_Promo Form_TEMPLATE.xlsx` in the project,
then `docker compose up -d --build app` — startup re-seeds (existing promos are untouched;
it upserts reference data).

**Logs:** `docker compose logs -f app` (or `oauth2-proxy`).

**Who's logged in (later):** oauth2-proxy passes the signed-in email to the app in the
`X-Auth-Request-Email` header — when we add per-department permissions, the app can read it.

---

## Troubleshooting

- **Login loop / "redirect_uri mismatch":** the Redirect URI in Entra must exactly equal
  `https://<APP_HOSTNAME>/oauth2/callback` (https, no trailing slash).
- **403 after login:** the account's email domain isn't `ALLOWED_EMAIL_DOMAIN`, or (if you
  set Assignment required) the user isn't assigned to the app in Enterprise applications.
- **502 from Caddy:** oauth2-proxy isn't reachable — confirm `CADDY_NETWORK` matches step 4
  and `docker compose ps` shows both containers up.
- **No HTTPS cert:** DNS isn't pointing at the droplet yet, or ports 80/443 aren't open.
- **Email claim empty:** some tenants don't return `email`; we use `preferred_username`
  (the UPN) which is reliable for Entra. Leave as-is unless sign-in is rejected.
