"""Deploy/delete a relay project on Vercel.

Ported from XHTTP-Installer's panel/server/services/vercel.service.ts
(https://github.com/avacocloud/XHTTP-Installer) to Python/httpx so SpiderPanel
can offer the same "connect my panel domain to a Vercel domain" flow from its
own System → Vercel Domain tab, without needing the Node/Express panel.

The uploaded project is the relay app at deploy/vercel/ next to this file: a
single serverless function (api/index.js) that proxies plain HTTP requests
under PUBLIC_RELAY_PATH to {TARGET_DOMAIN}{RELAY_PATH}. SpiderPanel bundles a
minimal relay runtime rather than the upstream optional decoy landing-page
bank. It is a pure HTTP relay (no WebSocket upgrade), which is why SpiderPanel
points it at its XHTTP transport, not its WS one.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx

VERCEL_API = "https://api.vercel.com"
RELAY_DIR = Path(__file__).resolve().parent / "deploy" / "vercel"

# Deploys can take a little longer than usual requests: the project carries a
# handful of landing-page templates (a few MB total) on top of the relay
# function itself.
_DEPLOY_TIMEOUT = httpx.Timeout(90.0, connect=15.0)
_DELETE_TIMEOUT = httpx.Timeout(30.0, connect=15.0)

_SKIP_DIR_NAMES = {"node_modules", ".vercel", ".git"}
# NOTICE.md is SpiderPanel-repo documentation (attribution for this directory's
# origin); it was not part of XHTTP-Installer's own deploy/vercel payload and
# has no reason to ride along inside the uploaded Vercel project.
_SKIP_FILE_NAMES = {"NOTICE.md"}


class VercelError(RuntimeError):
    """Raised with a human-readable message safe to show the admin."""


def _collect_files(base: Path) -> list[dict]:
    """Mirror vercel.service.ts's collectFiles(): every file under RELAY_DIR,
    base64-encoded, keyed by its path relative to RELAY_DIR (forward slashes,
    as the Vercel deployments API expects)."""
    files: list[dict] = []
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        if _SKIP_DIR_NAMES & set(path.relative_to(base).parts):
            continue
        if path.name in _SKIP_FILE_NAMES:
            continue
        rel = path.relative_to(base).as_posix()
        files.append({
            "file": rel,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "encoding": "base64",
        })
    return files


def _api_url(path: str, team_id: str | None) -> str:
    url = f"{VERCEL_API}{path}"
    if team_id:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}teamId={team_id}"
    return url


def _extract_vercel_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") or {}
        msg = err.get("message") or data.get("message")
        if msg:
            return str(msg)
    except Exception:
        pass
    return resp.text[:500]


async def deploy_to_vercel(
    token: str,
    project_name: str,
    target_domain: str,
    relay_path: str,
    public_path: str,
    team_id: str | None = None,
) -> dict:
    """Upload deploy/vercel/ as a new Vercel deployment and return
    {"url": "https://<domain>.vercel.app", "deploy_id": "...", "project_id": "..."}.

    Raises VercelError with a message safe to surface to the admin.
    """
    if not RELAY_DIR.is_dir():
        raise VercelError("Relay template directory (deploy/vercel) is missing from this install.")

    files = _collect_files(RELAY_DIR)
    if not files:
        raise VercelError("Relay template directory is empty.")

    target = target_domain if ":" in target_domain else f"{target_domain}:443"
    env = {
        "TARGET_DOMAIN": f"https://{target}",
        "RELAY_PATH": relay_path,
        "PUBLIC_RELAY_PATH": public_path,
    }
    body = {
        "name": project_name,
        "files": files,
        # Explicitly clear framework inference and give Vercel a real output
        # directory. vercel.json also declares this; keeping it here covers
        # deployments where project settings are used before config parsing.
        "projectSettings": {"framework": None, "outputDirectory": "public"},
        "env": env,
    }

    async with httpx.AsyncClient(timeout=_DEPLOY_TIMEOUT) as client:
        resp = await client.post(
            _api_url("/v13/deployments", team_id),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            content=json.dumps(body),
        )
        if resp.status_code >= 400:
            raise VercelError(f"Vercel deploy failed ({resp.status_code}): {_extract_vercel_error(resp)}")

        data = resp.json()
        deploy_id = str(data.get("id") or "").strip()
        domain = str(data.get("url") or "").strip()
        if not domain:
            raise VercelError("Vercel accepted the deployment but returned no domain.")
        if not deploy_id:
            raise VercelError("Vercel returned a deployment URL without an ID, so its build status cannot be verified.")

        # POST /v13/deployments returns as soon as the deployment is created,
        # not necessarily when its build succeeded. Wait for READY so SpiderPanel
        # never records a domain as connected while Vercel is still building or
        # has already failed.
        last_state = str(data.get("readyState") or data.get("status") or "QUEUED").upper()
        last_message = str(data.get("errorMessage") or "").strip()
        terminal_failure_states = {"ERROR", "CANCELED", "CANCELLED", "DELETED"}
        for attempt in range(30):
            if last_state == "READY":
                break
            if last_state in terminal_failure_states:
                detail = last_message or str(data.get("errorCode") or last_state)
                raise VercelError(f"Vercel deployment failed ({last_state}): {detail}")
            if attempt:
                await asyncio.sleep(2)
            status_resp = await client.get(
                _api_url(f"/v13/deployments/{deploy_id}", team_id),
                headers={"Authorization": f"Bearer {token}"},
            )
            if status_resp.status_code >= 400:
                raise VercelError(
                    f"Vercel created deployment {deploy_id}, but its status could not be checked "
                    f"({status_resp.status_code}): {_extract_vercel_error(status_resp)}"
                )
            data = status_resp.json()
            last_state = str(data.get("readyState") or data.get("status") or last_state).upper()
            last_message = str(data.get("errorMessage") or last_message or "").strip()
            if last_state == "READY":
                break
            if last_state in terminal_failure_states:
                detail = last_message or str(data.get("errorCode") or last_state)
                raise VercelError(f"Vercel deployment failed ({last_state}): {detail}")
        else:
            raise VercelError(
                f"Vercel deployment did not become READY within 60 seconds (last state: {last_state})."
            )

    return {
        "url": f"https://{domain}",
        "domain": domain,
        "deploy_id": deploy_id,
        "project_id": str(data.get("projectId") or (data.get("project") or {}).get("id") or ""),
    }


async def delete_vercel_project(token: str, project_name: str, team_id: str | None = None) -> None:
    """Delete the Vercel project. A 404 (already gone) is not an error."""
    async with httpx.AsyncClient(timeout=_DELETE_TIMEOUT) as client:
        resp = await client.delete(
            _api_url(f"/v9/projects/{project_name}", team_id),
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code >= 400 and resp.status_code != 404:
        raise VercelError(f"Vercel delete failed ({resp.status_code}): {_extract_vercel_error(resp)}")


async def verify_vercel_token(token: str) -> dict:
    """Light check that the token is usable before we attempt a deploy.
    Returns the Vercel user record on success; raises VercelError otherwise."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        resp = await client.get(
            f"{VERCEL_API}/v2/user",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code >= 400:
        raise VercelError(f"Vercel token rejected ({resp.status_code}): {_extract_vercel_error(resp)}")
    try:
        data = resp.json()
        return data.get("user") or data
    except Exception:
        return {}
