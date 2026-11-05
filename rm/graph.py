"""Microsoft Graph: SharePoint files and alert email.

Adapted from trybookinguk/s3reporting modules/utils/sharepoint.py and
email_utils.py (token, retry on 429/5xx, small upload, listing), plus the
item operations the SharePoint import needs: download by id, delete, move to
a sub-folder, create a folder, and send a plain alert email.

Needs the same Azure app as s3reporting (Sites.ReadWrite.All, Mail.Send).
"""
import logging
import time
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]
SMALL_UPLOAD_LIMIT = 4 * 1024 * 1024
MAX_RETRIES = 4
BACKOFF = 2


class GraphError(RuntimeError):
    pass


def _path(p: str) -> str:
    return quote(p.strip("/"), safe="/")


class Graph:
    def __init__(self, tenant_id, client_id, client_secret, drive_id=None, sender=None, session=None,
                 token=None, sleep=time.sleep):
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.drive_id, self.sender = drive_id, sender
        self.http = session or requests.Session()
        self._token = token
        self._sleep = sleep

    @classmethod
    def from_settings(cls, settings, **kw):
        return cls(settings.azure_tenant_id, settings.azure_client_id, settings.azure_client_secret,
                   drive_id=settings.sharepoint_drive_id, sender=settings.azure_sender_mailbox, **kw)

    # ------------------------------------------------------------- plumbing --
    def token(self) -> str:
        if self._token:
            return self._token
        import msal
        app = msal.ConfidentialClientApplication(
            self.client_id, authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret)
        result = app.acquire_token_for_client(scopes=SCOPE)
        if "access_token" not in result:
            raise GraphError("Graph sign-in failed: " + str(result.get("error_description") or result.get("error")))
        self._token = result["access_token"]
        return self._token

    def request(self, method: str, url: str, ok=(200, 201, 202, 204), **kw):
        if not url.startswith("http"):
            url = GRAPH + url
        headers = dict(kw.pop("headers", {}))
        timeout = kw.pop("timeout", 120)
        if "uploadUrl" not in url:
            headers["Authorization"] = f"Bearer {self.token()}"
        resp = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.http.request(method, url, headers=headers, timeout=timeout, **kw)
            except requests.exceptions.RequestException as e:
                if attempt == MAX_RETRIES - 1:
                    raise GraphError(f"{method} {url}: {e}") from e
                self._sleep(BACKOFF * 2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    wait = int(resp.headers.get("Retry-After", BACKOFF * 2 ** attempt))
                    log.warning("Graph %s %s -> %s, retrying in %ss", method, url, resp.status_code, wait)
                    self._sleep(wait)
                    continue
            break
        if ok is not None and resp.status_code not in ok:
            raise GraphError(f"{method} {url} -> {resp.status_code}: {resp.text[:300]}")
        return resp

    def _drive(self) -> str:
        if not self.drive_id:
            raise GraphError("SHAREPOINT_DRIVE_ID is not set")
        return f"/drives/{self.drive_id}"

    # ---------------------------------------------------------------- files --
    def list_folder(self, folder: str) -> list:
        """Items directly in a folder: [{id, name, size, is_folder, modified}]. [] if it doesn't exist."""
        url = f"{self._drive()}/root:/{_path(folder)}:/children?$top=200&$select=id,name,size,folder,file,lastModifiedDateTime"
        items = []
        while url:
            resp = self.request("GET", url, ok=(200, 404))
            if resp.status_code == 404:
                return []
            body = resp.json()
            for it in body.get("value", []):
                items.append({"id": it["id"], "name": it["name"], "size": it.get("size", 0),
                              "is_folder": "folder" in it, "modified": it.get("lastModifiedDateTime")})
            url = body.get("@odata.nextLink")
        return items

    def download(self, item_id: str) -> bytes:
        return self.request("GET", f"{self._drive()}/items/{item_id}/content", ok=(200,), timeout=600).content

    def delete(self, item_id: str) -> None:
        self.request("DELETE", f"{self._drive()}/items/{item_id}", ok=(204, 404))

    def ensure_folder(self, parent: str, name: str) -> None:
        self.request("POST", f"{self._drive()}/root:/{_path(parent)}:/children", ok=(200, 201, 409),
                     json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    def move(self, item_id: str, dest_folder: str, new_name: str = None) -> None:
        body = {"parentReference": {"path": f"/drive/root:/{dest_folder.strip('/')}"}}
        if new_name:
            body["name"] = new_name
        self.request("PATCH", f"{self._drive()}/items/{item_id}", ok=(200,), json=body)

    def upload_small(self, folder: str, name: str, data: bytes) -> None:
        if len(data) > SMALL_UPLOAD_LIMIT:
            raise GraphError(f"{name}: {len(data):,} bytes is over the 4 MB small-upload limit")
        self.request("PUT", f"{self._drive()}/root:/{_path(folder + '/' + name)}:/content", ok=(200, 201),
                     data=data, headers={"Content-Type": "text/plain"})

    # ----------------------------------------------------------------- mail --
    def send_mail(self, to: str, subject: str, body_text: str) -> None:
        if not self.sender:
            raise GraphError("AZURE_SENDER_MAILBOX is not set")
        msg = {"message": {"subject": subject, "body": {"contentType": "Text", "content": body_text},
                           "toRecipients": [{"emailAddress": {"address": a.strip()}} for a in to.split(",") if a.strip()]},
               "saveToSentItems": False}
        self.request("POST", f"/users/{quote(self.sender)}/sendMail", ok=(202,), json=msg)
