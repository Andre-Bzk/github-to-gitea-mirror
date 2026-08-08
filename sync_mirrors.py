#!/usr/bin/env python3
"""Mirror all GitHub repositories of a user into Gitea as pull mirrors.

Existing mirrors are left to Gitea's own cron; this script only creates the
ones that are missing and keeps the mirror interval aligned with the config.
It never deletes anything on the Gitea side -- a mirror that vanished upstream
is exactly what a backup is supposed to keep.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

KNOWN_KEYS = (
    "GITHUB_USER",
    "GITHUB_TOKEN",
    "GITEA_URL",
    "GITEA_TOKEN",
    "GITEA_OWNER",
    "MIRROR_INTERVAL",
    "INCLUDE_FORKS",
    "INCLUDE_ARCHIVED",
    "DRY_RUN",
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("MIRROR_ENV", os.path.join(SCRIPT_DIR, "mirror.env"))

errors = 0
last_headers = {}


def log(level, msg):
    print("%s [%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, msg), flush=True)


def fail(msg):
    global errors
    errors += 1
    log("ERROR", msg)


def load_config():
    cfg = {
        "GITHUB_USER": "",
        "GITHUB_TOKEN": "",
        "GITEA_URL": "http://127.0.0.1:3000",
        "GITEA_TOKEN": "",
        "GITEA_OWNER": "",
        "MIRROR_INTERVAL": "24h",
        "INCLUDE_FORKS": "false",
        "INCLUDE_ARCHIVED": "true",
        "DRY_RUN": "false",
    }
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key in KNOWN_KEYS:
                    cfg[key] = value.strip().strip('"').strip("'")
    else:
        log("WARN", "No config file at %s, relying on environment" % CONFIG_PATH)

    for key in KNOWN_KEYS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]

    cfg["GITEA_URL"] = cfg["GITEA_URL"].rstrip("/")
    return cfg


def truthy(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def api(url, token=None, scheme="token", method="GET", payload=None):
    """Return (status, parsed_body). Raises urllib.error.HTTPError on >=400."""
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "gitea-github-mirror/1.0",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "%s %s" % (scheme, token)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read().decode("utf-8")
        last_headers.clear()
        last_headers.update({k.lower(): v for k, v in resp.headers.items()})
        return resp.status, (json.loads(body) if body.strip() else None)


def github_repos(cfg):
    """All repos owned by the user; private ones included when a token is set."""
    token = cfg["GITHUB_TOKEN"]
    repos = []
    page = 1
    while True:
        if token:
            url = ("https://api.github.com/user/repos"
                   "?per_page=100&affiliation=owner&page=%d" % page)
        else:
            url = ("https://api.github.com/users/%s/repos"
                   "?per_page=100&type=owner&page=%d" % (cfg["GITHUB_USER"], page))
        _, batch = api(url, token=token, scheme="Bearer")
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    if token:
        # affiliation=owner still returns repos owned by orgs the user belongs to
        repos = [r for r in repos
                 if r["owner"]["login"].lower() == cfg["GITHUB_USER"].lower()]
    return repos


def warn_token_expiry():
    """Fine-grained PATs always expire; a silent expiry would stall the mirror.

    GitHub reports the date in a response header, so every run can check it.
    """
    raw = last_headers.get("github-authentication-token-expiration")
    if not raw:
        return  # classic token without expiry, or no token at all
    text = raw.strip().replace(" UTC", "").replace("Z", "").replace("T", " ")
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text.split(".")[0], fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    days = (parsed.replace(tzinfo=None) - now_utc).days
    if days < 0:
        fail("GITHUB_TOKEN expired on %s - renew it" % parsed.date())
    elif days <= 14:
        log("WARN", "GITHUB_TOKEN expires in %d day(s), on %s - renew it"
            % (days, parsed.date()))
    else:
        log("INFO", "GITHUB_TOKEN valid until %s (%d days)" % (parsed.date(), days))


def duration_seconds(value):
    """Parse a Go-style duration ('24h', '8h0m0s') into seconds. -1 if unparseable."""
    if not value:
        return -1
    total = 0
    matched = False
    for amount, unit in re.findall(r"(\d+)\s*([hms])", str(value)):
        matched = True
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total if matched else -1


def gitea_repo(cfg, name):
    try:
        _, repo = api("%s/api/v1/repos/%s/%s" % (cfg["GITEA_URL"], cfg["GITEA_OWNER"], name),
                      token=cfg["GITEA_TOKEN"])
        return repo
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def create_mirror(cfg, repo, dry_run):
    name = repo["name"]
    payload = {
        "clone_addr": repo["clone_url"],
        "repo_name": name,
        "repo_owner": cfg["GITEA_OWNER"],
        "mirror": True,
        "mirror_interval": cfg["MIRROR_INTERVAL"],
        "private": bool(repo.get("private")),
        "description": (repo.get("description") or "")[:255],
        "service": "github",
        # Pull mirrors only ever sync git data, so importing the rest would be a
        # one-off snapshot that silently goes stale. Keep it lean on the Pi.
        "wiki": False,
        "issues": False,
        "labels": False,
        "milestones": False,
        "releases": False,
        "pull_requests": False,
    }
    if cfg["GITHUB_TOKEN"]:
        payload["auth_token"] = cfg["GITHUB_TOKEN"]

    if dry_run:
        log("DRY", "would mirror %s (private=%s)" % (name, payload["private"]))
        return

    api("%s/api/v1/repos/migrate" % cfg["GITEA_URL"], token=cfg["GITEA_TOKEN"],
        method="POST", payload=payload)
    log("OK", "mirrored %s (private=%s)" % (name, payload["private"]))


def align_interval(cfg, name, existing, dry_run):
    wanted = duration_seconds(cfg["MIRROR_INTERVAL"])
    current = duration_seconds(existing.get("mirror_interval"))
    if wanted < 0 or current < 0 or wanted == current:
        return
    if dry_run:
        log("DRY", "would set %s interval %s -> %s"
            % (name, existing.get("mirror_interval"), cfg["MIRROR_INTERVAL"]))
        return
    api("%s/api/v1/repos/%s/%s" % (cfg["GITEA_URL"], cfg["GITEA_OWNER"], name),
        token=cfg["GITEA_TOKEN"], method="PATCH",
        payload={"mirror_interval": cfg["MIRROR_INTERVAL"]})
    log("OK", "interval of %s set to %s" % (name, cfg["MIRROR_INTERVAL"]))


def main():
    cfg = load_config()
    dry_run = truthy(cfg["DRY_RUN"])

    missing = [k for k in ("GITHUB_USER", "GITEA_URL", "GITEA_TOKEN", "GITEA_OWNER")
               if not cfg[k]]
    if missing:
        log("ERROR", "Missing config value(s): %s" % ", ".join(missing))
        return 2

    if not cfg["GITHUB_TOKEN"]:
        log("WARN", "GITHUB_TOKEN is empty - private repositories will be skipped")

    try:
        repos = github_repos(cfg)
    except urllib.error.HTTPError as exc:
        log("ERROR", "GitHub API %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:300]))
        return 2
    except Exception as exc:
        log("ERROR", "GitHub API unreachable: %s" % exc)
        return 2

    warn_token_expiry()  # must run before any Gitea call overwrites the headers

    if not truthy(cfg["INCLUDE_FORKS"]):
        repos = [r for r in repos if not r.get("fork")]
    if not truthy(cfg["INCLUDE_ARCHIVED"]):
        repos = [r for r in repos if not r.get("archived")]

    private_count = sum(1 for r in repos if r.get("private"))
    log("INFO", "GitHub: %d repositor%s to consider (%d public, %d private)%s"
        % (len(repos), "y" if len(repos) == 1 else "ies",
           len(repos) - private_count, private_count,
           " (DRY RUN)" if dry_run else ""))

    # A fine-grained token defaults to "Public repositories", which looks exactly
    # like an account without private repos. Name the likely cause explicitly.
    if cfg["GITHUB_TOKEN"] and private_count == 0:
        log("WARN", "Token is set but GitHub reports no private repositories - "
                    "check that its repository access is 'All repositories', "
                    "not 'Public repositories'")

    created = skipped = 0
    for repo in sorted(repos, key=lambda r: r["name"].lower()):
        name = repo["name"]
        try:
            existing = gitea_repo(cfg, name)
        except Exception as exc:
            fail("%s: cannot query Gitea: %s" % (name, exc))
            continue

        if existing is None:
            try:
                create_mirror(cfg, repo, dry_run)
                created += 1
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                fail("%s: migrate failed (HTTP %s): %s" % (name, exc.code, detail))
            except Exception as exc:
                fail("%s: migrate failed: %s" % (name, exc))
            continue

        skipped += 1
        if not existing.get("mirror"):
            log("WARN", "%s exists in Gitea but is not a mirror - left untouched" % name)
            continue
        try:
            align_interval(cfg, name, existing, dry_run)
        except Exception as exc:
            fail("%s: could not update mirror interval: %s" % (name, exc))

    github_names = {r["name"].lower() for r in repos}
    try:
        page, orphans = 1, []
        while True:
            _, batch = api("%s/api/v1/user/repos?limit=50&page=%d" % (cfg["GITEA_URL"], page),
                           token=cfg["GITEA_TOKEN"])
            if not batch:
                break
            orphans.extend(
                r["name"] for r in batch
                if r.get("mirror")
                and r["owner"]["login"].lower() == cfg["GITEA_OWNER"].lower()
                and r["name"].lower() not in github_names
            )
            if len(batch) < 50:
                break
            page += 1
        if orphans:
            log("WARN", "Mirrors with no GitHub counterpart (renamed or deleted upstream): %s"
                % ", ".join(sorted(orphans)))
    except Exception as exc:
        log("WARN", "Could not check for orphaned mirrors: %s" % exc)

    log("INFO", "Done: %d created, %d already present, %d error(s)" % (created, skipped, errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
