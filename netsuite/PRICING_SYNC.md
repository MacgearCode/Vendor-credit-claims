# Automated pricing sync — NetSuite → app

Keeps product/customer pricing current by pulling the master price level saved
searches from NetSuite on a schedule and upserting them into the app.

```
n8n (daily)
  ├─ GET  NetSuite RESTlet ?searchId=customsearch1084   (AU master price levels)
  ├─ POST http://claims:8000/admin/sync-pricing  {country:"AU", rows:[…]}
  ├─ GET  NetSuite RESTlet ?searchId=customsearch1413   (NZ master price levels)
  └─ POST http://claims:8000/admin/sync-pricing  {country:"NZ", rows:[…]}
```

**NetSuite is the source of truth for the product list and pricing.** Each sync makes
the app's catalog for that country match the feed: it adds new SKUs, updates RRP + every
channel/retailer price, and **prunes discontinued SKUs** (codes no longer in the feed).
Deleting a product is safe — promos store the code as text, so existing promos are
unaffected. A safety guard skips pruning if a feed comes back suspiciously small
(< 50 rows), so a broken/empty NetSuite response can never wipe the catalog.

The bundled template is only a **bootstrap**: on a fresh, empty database the app seeds
products from it so the app isn't blank before the first sync. Once products exist,
restarts never reseed from the template — NetSuite owns the catalog.

> Saved searches: **AU = `customsearch1084`**, **NZ = `customsearch1413`**.
> They already return exactly the columns the importer expects (Code, Description,
> Brand, AU/NZ Status, Base (RRP Inc), then one column per retailer channel).

---

## 1. Deploy the RESTlet in NetSuite

1. **Customization → Scripting → Scripts → New.**
2. Upload `netsuite/run_saved_search_restlet.js` (from this repo) as the script file.
   Type = **RESTlet**. Functions: GET = `get`, POST = `post`. Save.
3. **Deploy Script:** status **Released**, and set the audience/role to one that can
   run the searches (e.g. a dedicated integration role with "Lists > Items" view +
   search permissions). Save.
4. From the deployment, copy the **External URL**. It looks like:
   ```
   https://<ACCOUNTID>.restlets.api.netsuite.com/app/site/hosting/restlet.nl?script=<SCRIPTID>&deploy=<DEPLOYID>
   ```
   You'll append `&searchId=customsearch1084` (or `1413`) when calling it.

Quick manual test (in browser you can't, but) — once tokens exist (step 2), n8n or
`curl` with OAuth1 should return a JSON array of rows.

---

## 2. Create token-based auth (TBA) credentials

1. **Setup → Company → Enable Features → SuiteCloud:** ensure **Token-Based
   Authentication** and **RESTlets** are enabled.
2. **Setup → Integration → Manage Integrations → New:** name e.g.
   "Vendor Credit Claims pricing sync". Untick TBA: Authorization Flow / OAuth2; tick
   **Token-Based Authentication**. Save → copy the **Consumer Key** and **Consumer
   Secret** (shown once).
3. **Setup → Users/Roles → Access Tokens → New:** pick the integration above, a user,
   and a role that can run the searches. Save → copy the **Token ID** and **Token
   Secret** (shown once).
4. Note your **Account ID** (realm), e.g. `1234567` (or `1234567_SB1` for sandbox).

You now have the four secrets n8n needs: consumer key/secret, token id/secret (+ account id).

---

## 3. Set the app's shared secret

On the droplet:
```bash
cd /opt/vendor-credit-claims/deploy
# generate a secret and put it in .env
echo "PRICING_SYNC_TOKEN=$(openssl rand -hex 24)" >> .env   # or edit .env by hand
docker compose -f docker-compose.basic.yml up -d            # recreate to pick up the env
```
The endpoint rejects every call unless this is set and matched.

---

## 4. Build the n8n workflow (Code node — do NOT use n8n's OAuth1 credential)

n8n's built-in **OAuth1 API** credential does **not** fit NetSuite TBA — it's for the
3-legged OAuth1 handshake and offers no realm field and only HMAC-SHA1. NetSuite TBA needs
a pre-issued token, the **realm** (Account ID), and **HMAC-SHA256**. So we sign the request
ourselves in a **Code node**, which also does the POST to the app — one node, no auth UI.

Two nodes total:

1. **Schedule Trigger** — e.g. daily at 06:00.
2. **Code node** (mode: *Run Once for All Items*) — paste `netsuite/n8n_sync_code_node.js`
   from this repo and fill in the constants at the top:
   - `ACCOUNT_ID` (Setup → Company → Company Information → **ACCOUNT ID**)
   - `CONSUMER_KEY` / `CONSUMER_SECRET` (from the Integration record, step 2)
   - `TOKEN_ID` / `TOKEN_SECRET` (from the Access Token, step 2)
   - `RESTLET_SCRIPT` / `RESTLET_DEPLOY` (the `script=` and `deploy=` numbers in the RESTlet
     deployment URL from step 1)
   - `SYNC_TOKEN` (the `PRICING_SYNC_TOKEN` from step 3)

   It signs OAuth1, pulls both searches (`customsearch1084` AU, `customsearch1413` NZ) via
   the RESTlet, and POSTs each to `http://claims:8000/admin/sync-pricing`. The node returns
   one summary per country, e.g. `{"country":"AU","received":1760,"inserted":3,"updated":1757,"pruned":2}`.

   `http://claims:8000` works because n8n and the app share the Docker network, bypassing
   Caddy and the app's login entirely — `/admin/sync-pricing` authenticates on the
   `X-Sync-Token` header alone, not on a user session. That is why renaming the public
   hostname (promos → apps, 2026-08-05) and adding per-user accounts both left this sync
   untouched. If your n8n ever runs elsewhere, set `APP_URL` to
   `https://apps.macgeargroup.com/admin/sync-pricing`; the token header still carries it,
   no user credentials needed.

> Prefer not to store secrets in the node? Set them as environment variables on the n8n
> container and replace each constant with e.g. `$env.NETSUITE_CONSUMER_KEY`.

---

## 5. Verify

Run the workflow manually once. Then in the app, open a promo and check a SKU's
buy/channel price reflects NetSuite. The Sync node's response shows how many rows were
inserted/updated.

---

## Notes

- **NetSuite is authoritative for products.** Template product seeding only happens on an
  empty catalog (bootstrap). The daily sync adds/updates/removes SKUs to match NetSuite.
- **Pruning guard:** if a sync receives < 50 rows for a country it upserts but does **not**
  prune (the response shows `"prune_skipped": true`). You can also send `"prune": false`
  in the POST body to update-only without removing anything.
- **Retailers/rebates** still come from the templates (they change rarely). If the retailer
  list/rebates should also sync from NetSuite, that's a second saved search + a small addition.
- The endpoint only accepts `country` AU or NZ and requires the `X-Sync-Token` header.
- The sync response (e.g. `{"inserted":12,"updated":1748,"pruned":5,...}`) is worth logging
  in n8n so you can see daily catalog churn and get alerted if `pruned` spikes.
