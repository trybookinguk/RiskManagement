# Temporary remote access through ngrok

Use this **until the router forwards port 8443** (see `entra_sso_setup.md`
step 5). ngrok runs as a third container on the NAS. It connects **out** to
ngrok, so you don't need router changes, port forwarding or DSM reverse proxy
settings. You can do all of it remotely from DSM (Task Scheduler and File
Station).

**What's different from the router set-up:**
- **The dashboard is on the internet** at `https://<your-name>.ngrok-free.app`.
  Microsoft SSO with MFA, plus the app roles, still guard everything. Before
  sign-in, nothing is shown except the sign-in page.
- **ngrok decrypts the traffic** on its servers before passing it on, so risk
  data (account names, scores, balances) passes through ngrok. That's
  acceptable for a short period. **Stop the tunnel once the router is done.**
- **There's no UK-only filter** (that's the DSM firewall rule in the router
  set-up).

## 1. ngrok account (free)

1. Sign up at <https://dashboard.ngrok.com>, using a TryBooking email address.
2. Go to **Your Authtoken** and copy the token. Keep it in the password
   manager.
3. Go to **Domains → New Domain**. The free plan gives one fixed domain, e.g.
   `pale-fox-1234.ngrok-free.app`. Copy it.

## 2. Settings on the NAS

1. Run the task `rmctl.sh env-export`.
2. In File Station, open **riskmanagement-data → setup → env-edit.txt** and
   set:

   ```
   NGROK_AUTHTOKEN=<token from step 1>
   NGROK_DOMAIN=pale-fox-1234.ngrok-free.app
   WEB_BASE_URL=https://pale-fox-1234.ngrok-free.app
   ```

3. Run the task `rmctl.sh env-apply`.

## 3. Entra redirect URI

In the app registration **RiskManagement Dashboard → Authentication → Web →
Add URI**, add:

```
https://pale-fox-1234.ngrok-free.app/auth/callback
```

Keep the `trybooking.synology.me:8443` one too, so you can switch back later
without another admin change. If the Entra app isn't set up yet, the tunnel
still works. The page will just say Microsoft sign-in isn't set up.

## 4. Start

Run these tasks in order:

1. `rmctl.sh update`
2. `rmctl.sh web-start`
3. `rmctl.sh tunnel-start`. It ends with "Tunnel is up: https://…".

Then open `https://pale-fox-1234.ngrok-free.app` from anywhere:

- The free plan first shows an ngrok notice page. Click **Visit Site**; it
  appears once per browser.
- Sign in with Microsoft.
- Open `/api/me` to check your role and MFA.

The tunnel restarts on its own if the NAS reboots, until you stop it.

## 5. Switch to the router set-up later

1. `rmctl.sh tunnel-stop`
2. `env-export`, then set `WEB_BASE_URL=https://trybooking.synology.me:8443`
   and blank out the `NGROK_*` lines, then `env-apply`.
3. Remove the ngrok redirect URI in Entra, and delete the domain and authtoken
   in the ngrok dashboard.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `tunnel-start` says the tunnel didn't start | Check the authtoken and that the domain is exactly the one in ngrok → Domains. |
| "unknown host" | `WEB_BASE_URL` isn't `https://<NGROK_DOMAIN>`. Fix it with env-export / env-apply. |
| Microsoft says "redirect URI mismatch" | Add the step 3 URI in Entra. It must match exactly. |
| Scripts calling `/api/...` get an ngrok HTML page | Add the header `ngrok-skip-browser-warning: 1` to the request. |
| Check it's running | `rmctl.sh tunnel-status` |
