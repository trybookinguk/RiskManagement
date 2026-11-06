# Dashboard and API sign-in: Microsoft SSO with MFA

The dashboard (`https://trybooking.synology.me:8443`) and its JSON API (`/api/...`)
accept only:

- people in the **TryBooking Microsoft tenant** with an **@trybooking.co.uk**
  account,
- who hold the app role **Risk Viewer** or **Risk Admin**, and
- who **completed MFA**. A Conditional Access policy enforces it, and the app
  checks the token too.

There are no passwords or static tokens. Removing someone's role in Entra
locks them out at their next sign-in, within 8 hours at most.

Steps 1–4 are for the **Microsoft 365 / Entra admin** (Entra ID P1 is
needed). Steps 5–8 are on the **NAS** (DSM). Step 9 tests it.

---

## 1. App registration

Go to the [Entra admin center](https://entra.microsoft.com), then **Identity →
Applications → App registrations → New registration**.

| Field | Value |
| --- | --- |
| Name | `RiskManagement Dashboard` |
| Supported account types | **Accounts in this organizational directory only** (single tenant) |
| Redirect URI | Platform **Web**, `https://trybooking.synology.me:8443/auth/callback` |

After **Register**, copy the **Application (client) ID** and the
**Directory (tenant) ID** from the Overview page.

Then:

1. **Certificates & secrets → Client secrets → New client secret.** Call it
   `NAS dashboard` and pick 24 months. Copy the **Value** straight away; it's
   shown only once. Put a reminder in the calendar a month before it expires.
2. **App roles → Create app role**, twice:

   | Display name | Allowed member types | Value | Description |
   | --- | --- | --- | --- |
   | Risk Viewer | Users/Groups | `Risk.Viewer` | View the risk dashboard and API |
   | Risk Admin | Users/Groups | `Risk.Admin` | Viewer plus the Imports page |

3. **Expose an API.**
   - **Application ID URI → Add**, and accept `api://<client id>`.
   - **Add a scope:** name `Risk.Read`, who can consent **Admins only**,
     display name "Read the risk dashboard", state Enabled.
4. **API permissions → Grant admin consent for TryBooking.** This covers the
   default `User.Read`.

Leave token versions and optional claims at their defaults. The app accepts
both v1 and v2 tokens.

## 2. Who can sign in

Go to **Identity → Applications → Enterprise applications → RiskManagement
Dashboard**:

1. **Properties → Assignment required? = Yes → Save.** Nobody else can get a
   token for the app.
2. **Users and groups → Add user/group.** Ideally use two security groups:
   - `RiskManagement Users` with role **Risk Viewer**
   - `RiskManagement Admins` with role **Risk Admin**

## 3. MFA: Conditional Access (Entra ID P1)

**3a. Authentication context.** Go to **Protection → Conditional Access →
Authentication contexts → New authentication context**:

- Name `RiskManagement MFA`
- ID **`c1`**. If c1 is already used, pick another and set
  `WEB_AUTH_CONTEXT` to match.
- Tick **Publish to apps** → Save.

**3b. Policy on the app.** Go to **Policies → New policy**:

| Setting | Value |
| --- | --- |
| Name | `RiskManagement - require MFA` |
| Users | Include the two groups above (or All users) |
| Target resources | Cloud apps → Select → **RiskManagement Dashboard** |
| Grant | **Require multifactor authentication** (or the "Multifactor authentication" authentication strength) |
| Session | Sign-in frequency **8 hours** (optional; matches the dashboard session) |
| Enable policy | **On** (or Report-only first, then On) |

**3c. Policy on the context.** Create another new policy that's the same as
3b, except:
- Name `RiskManagement - MFA context c1`
- Target resources: **Authentication context → `RiskManagement MFA (c1)`**

**Why two policies:** 3b makes Microsoft demand MFA for any sign-in to the
app. 3c means that when the dashboard asks for context `c1`, Microsoft
stamps `acrs: c1` in the token after MFA. The dashboard then checks that
stamp itself, or the `amr: mfa` claim (`WEB_MFA_CHECK=amr_or_acrs`). A
sign-in that shows neither is refused with "Multi-factor authentication is
required".

## 4. Hand over to the NAS admin

Hand these over through the password manager, not by email:
- the tenant ID
- the client ID
- the client secret value

---

## 5. Address: Synology DDNS, reachable from anywhere

The dashboard's address is **`https://trybooking.synology.me:8443`**. It works
in the office and remotely. It uses the NAS's free Synology DDNS name, so no
company DNS record is needed.

**What protects it from the internet:**
- **Microsoft SSO:** TryBooking tenant, @trybooking.co.uk, app role, and MFA
  enforced by Conditional Access and checked by the app.
- **DSM firewall:** port 8443 only from the UK (step 7).
- **Before sign-in, the app shows nothing:** only the sign-in page and
  `/api/health` (`{"ok": true}`) answer. Requests for any other host name,
  such as scanners hitting the bare IP, are refused.
- **Only 8443 is forwarded.** DSM itself (5000/5001), SSH and the dashboard's
  internal port 8085 stay private.

Steps:
1. In **DSM → Control Panel → External Access → DDNS**, check that
   `trybooking.synology.me` shows **Normal** with **Heartbeat** on.
2. **Router:** forward **TCP 8443** (external) to the **NAS LAN IP, port
   8443**. Forward nothing else, apart from port 80 if step 6 needs it.
3. **In the office:** browsers use the same address through the router (NAT
   loopback). If your router doesn't support loopback, the page won't load
   from the office. In that case, add a local DNS entry on the router
   (`trybooking.synology.me` → NAS LAN IP), or a hosts-file line on the PCs:
   `<NAS LAN IP>  trybooking.synology.me`

## 6. Certificate (Let's Encrypt via DSM)

1. Go to **DSM → Control Panel → Security → Certificate**. If there's no
   certificate for `trybooking.synology.me` (the DDNS wizard usually makes
   one), choose **Add → Add a new certificate → Get a certificate from Let's
   Encrypt**, with domain `trybooking.synology.me` and email
   `henry@trybooking.co.uk`.
2. DSM renews it automatically. For synology.me names, recent DSM versions do
   this without opening any port. **If DSM reports a Let's Encrypt error**, also
   forward **TCP 80** to the NAS.
   - Don't country-limit port 80. Let's Encrypt checks from several countries.

## 7. Reverse proxy and firewall

**Reverse proxy.** Go to **DSM → Control Panel → Login Portal → Advanced →
Reverse Proxy → Create**:

| | Protocol | Hostname | Port |
| --- | --- | --- | --- |
| Source | HTTPS | `trybooking.synology.me` | **8443** |
| Destination | HTTP | `localhost` | 8085 |

Tick **Enable HSTS**. Then in **Security → Certificate → Settings**, set this
entry to use the `trybooking.synology.me` certificate.

**Firewall.** Go to **DSM → Control Panel → Security → Firewall → Enable
firewall → Edit rules** (on the profile used by the LAN interface). Put these
rules in this order, above any existing allow-all rule:

| # | Ports | Source | Action |
| --- | --- | --- | --- |
| 1 | All | Your office LAN subnet, e.g. `192.168.1.0/24` | Allow |
| 2 | Custom: TCP 8443 | **Location: United Kingdom** | Allow |
| 3 | Custom: TCP 8443 | All | **Deny** |
| 4 | Custom: TCP 80 (only if forwarded for step 6) | All | Allow |

Keep your existing rules for DSM and other services below these. Check that rule
1 really covers your LAN before saving, or you could lock yourself out of DSM.
If people travel abroad, add their countries to rule 2, or remove rule 3 while
they're away. MFA still applies either way.

**Check from outside:** on a phone on mobile data (wifi off), open
`https://trybooking.synology.me:8443`. You should get the sign-in page with a
padlock. Then try `https://trybooking.synology.me:5001` (DSM). That should
**not** connect.

## 8. Settings and start

1. Run the task `rmctl.sh env-export`.
2. In File Station, open **riskmanagement-data → setup → env-edit.txt** with
   Text Editor and set:

   ```
   AZURE_TENANT_ID=<directory (tenant) id>        # usually already set (same tenant)
   WEB_ENTRA_CLIENT_ID=<application (client) id>
   WEB_ENTRA_CLIENT_SECRET=<client secret value>
   WEB_BASE_URL=https://trybooking.synology.me:8443
   ```

3. Run the task `rmctl.sh env-apply`.
4. Run the task `rmctl.sh web-start`. It ends with "Dashboard is up".

## 9. Test

1. Browse to `https://trybooking.synology.me:8443` and click **Sign in with
   Microsoft**. You should be asked for MFA, then land on the risk list.
2. Open `https://trybooking.synology.me:8443/api/me`. It shows your role and how MFA
   was proven, e.g. `"mfa": "acrs:c1"` or `"mfa": "amr"`.
3. Check what a non-MFA sign-in does. Use an account without the role, or a
   private window with Conditional Access in Report-only. It should be refused
   with a clear message.

**If every sign-in is refused with "Multi-factor authentication is
required"** even though you did MFA, the tokens aren't carrying the MFA claim.
Check that the authentication context `c1` is published and that policy 3c is
**On**. As a last resort, set `WEB_MFA_CHECK=off` with `env-export` /
`env-apply`. Conditional Access policy 3b still enforces MFA, but the
dashboard no longer double-checks it.

---

## Using the API

Every page has a JSON twin under `/api/...`.

- **In the browser:** once you're signed in, open the `/api/...` addresses
  directly.
- **From a script on your own PC:** the script sends a Microsoft access token
  for `api://<client id>/Risk.Read`. One way is the Azure CLI, which needs a
  one-off admin step: **Expose an API → Add a client application**, client ID
  `04b07795-8ddb-461a-bbee-02f9e1bf7b46` (Microsoft Azure CLI), scope
  `Risk.Read`. Then:

  ```bash
  az login --tenant <tenant id>
  ```

  ```bash
  TOKEN=$(az account get-access-token --scope api://<client id>/Risk.Read --query accessToken -o tsv)
  ```

  ```bash
  curl -H "Authorization: Bearer $TOKEN" https://trybooking.synology.me:8443/api/risk?priority=P1
  ```

  Conditional Access applies, so `az login` asks for MFA.

Tokens without a signed-in user (app-only, client credentials) are refused.
There are no unattended jobs. If one is ever needed, it has to be added
deliberately, with its own role.
