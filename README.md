# zoho-email-search

A small Streamlit app for searching Zoho CRM's logged email history by
recipient address or by domain — something Zoho's own UI and API don't
support directly. Search Contacts, Leads, and Accounts, pick a date range,
and export a combined, deduplicated XLSX or PDF report.

It searches in two stages under the hood, because Zoho has no global email
search:

1. **Match records** — a COQL query against Contacts, Leads, and Accounts
   for any record whose `Email` field matches the search value (exact
   address, or a `%domain%` wildcard).
2. **Pull email logs** — for every matched record, call Zoho's
   *Get Emails of a Record* API, paginate through the results, then merge,
   deduplicate, and filter to the chosen date range.

## One-time setup: Zoho self-client credentials

This app authenticates as a **Zoho self-client** — a set of OAuth
credentials you generate once from your own Zoho login, with no redirect
flow needed. Do this in the **EU** console, since this org's CRM data lives
on Zoho's EU data center:

1. Go to **https://api-console.zoho.eu** and log in with your Zoho account.
2. Create a new **Self Client**.
3. In its **Generate Code** tab, request these scopes (all read-only —
   this app never writes to CRM):
   - `ZohoCRM.coql.READ`
   - `ZohoCRM.modules.contacts.READ`
   - `ZohoCRM.modules.leads.READ`
   - `ZohoCRM.modules.accounts.READ`
   - `ZohoCRM.modules.emails.READ`
4. Generate the code. It's short-lived (a few minutes), so exchange it
   immediately for tokens:

   ```bash
   curl -X POST https://accounts.zoho.eu/oauth/v2/token \
     -d grant_type=authorization_code \
     -d client_id=YOUR_CLIENT_ID \
     -d client_secret=YOUR_CLIENT_SECRET \
     -d redirect_uri=YOUR_REDIRECT_URI \
     -d code=YOUR_GENERATED_CODE
   ```

   The response includes an `access_token` (expires in ~1 hour — not
   needed again) and a `refresh_token` (long-lived — this is what the app
   actually uses day to day).

5. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and
   fill in `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, and
   a shared `APP_PASSWORD` of your choosing.

Zoho rate-limits how often you can generate a fresh grant code per day, so
get this right once rather than regenerating repeatedly.

### If the refresh token is ever revoked

Repeat steps 3–4 above to generate a new grant code and exchange it for a
new refresh token, then update `ZOHO_REFRESH_TOKEN` in
`.streamlit/secrets.toml` (locally) and in Streamlit Cloud's **Secrets**
panel (for the deployed app). Nothing else needs to change.

## Running locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploying

Push this repo to GitHub, connect it in Streamlit Cloud, and paste the
contents of your `secrets.toml` into the app's **Secrets** panel there
(the real `secrets.toml` is gitignored and never committed).

## Tuning knobs

All in `constants.py`:

- `MAX_MATCHED_RECORDS` — hard cap on how many Contacts/Leads/Accounts a
  single search will match before it stops (protects against a very broad
  domain search pulling thousands of records' email histories). Default 500.
- `MAX_WORKERS` — thread pool size for Stage 2's per-record email pulls.
  Default 4.
- `MAX_DISPLAY_ROWS` — cap on rows shown in the in-app results table (the
  full result set is always included in the XLSX/PDF export regardless).
  Default 500.
- `DEFAULT_EMAIL_TYPES` / `ALL_EMAIL_TYPES` — which Zoho email "types"
  (`sent_from_crm`, `scheduled_in_crm`, `drafts`, `user_emails`) are
  searched by default vs. offered as options.

## Project layout

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI — search form, progress, results, exports |
| `auth_gate.py` | Shared-password login gate |
| `zoho_auth.py` | OAuth token refresh/caching |
| `zoho_client.py` | COQL + Get-Emails-of-Record API wrappers, retry/backoff |
| `search.py` | Two-stage search orchestration, progress callbacks |
| `email_log.py` | Normalizes raw Zoho JSON into a canonical schema, merge/dedupe |
| `export.py` | Styled XLSX/PDF report builders |
| `constants.py` | Shared config and tuning knobs |
