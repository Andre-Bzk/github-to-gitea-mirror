#!/usr/bin/env python3
"""Mirror all GitHub repositories of a user into Gitea as pull mirrors.

Existing git mirrors are left to Gitea's own cron. This script creates the ones
that are missing, aligns their interval, and synchronizes GitHub release
metadata and assets because Gitea's native mirror does not. It never deletes
anything on the Gitea side -- retained history is the point of a backup.
"""

import json
import http.client
import io
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
    "SYNC_RELEASES",
    "SYNC_RELEASE_ASSETS",
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
        "SYNC_RELEASES": "true",
        "SYNC_RELEASE_ASSETS": "true",
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
        "User-Agent": "github-to-gitea-mirror/1.0",
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


def path_part(value):
    """Quote one URL path component, including slashes used inside tag names."""
    return urllib.parse.quote(str(value), safe="")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward API credentials to a cross-host download redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new_host = urllib.parse.urlsplit(newurl).netloc.lower()
        if old_host != new_host:
            redirected.remove_header("Authorization")
        return redirected


safe_download_opener = urllib.request.build_opener(SafeRedirectHandler())


def download_github_asset(cfg, asset):
    """Download a release asset into a temporary file without buffering it in RAM."""
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "github-to-gitea-mirror/1.0",
    }
    if cfg["GITHUB_TOKEN"]:
        headers["Authorization"] = "Bearer %s" % cfg["GITHUB_TOKEN"]
    req = urllib.request.Request(asset["url"], headers=headers)
    tmp = tempfile.TemporaryFile()
    size = 0
    try:
        with safe_download_opener.open(req, timeout=180) as resp:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
                size += len(chunk)
        expected = int(asset.get("size") or 0)
        if expected and size != expected:
            raise IOError("downloaded %d bytes, expected %d" % (size, expected))
        tmp.seek(0)
        return tmp, size
    except Exception:
        tmp.close()
        raise


def upload_release_asset(cfg, repo_name, release_id, asset, source, size):
    """Stream a temporary file to Gitea as multipart/form-data."""
    query = urllib.parse.urlencode({"name": asset["name"]})
    url = ("%s/api/v1/repos/%s/%s/releases/%s/assets?%s"
           % (cfg["GITEA_URL"], path_part(cfg["GITEA_OWNER"]), path_part(repo_name),
              release_id, query))
    parsed = urllib.parse.urlsplit(url)
    boundary = "----github-to-gitea-mirror-%s" % uuid.uuid4().hex
    safe_name = str(asset["name"]).replace("\\", "_").replace('"', "_")
    safe_name = safe_name.replace("\r", "_").replace("\n", "_")
    content_type = asset.get("content_type") or "application/octet-stream"
    content_type = str(content_type).replace("\r", "").replace("\n", "")
    preamble = (
        "--%s\r\n"
        "Content-Disposition: form-data; name=\"attachment\"; filename=\"%s\"\r\n"
        "Content-Type: %s\r\n\r\n" % (boundary, safe_name, content_type)
    ).encode("utf-8")
    ending = ("\r\n--%s--\r\n" % boundary).encode("ascii")
    request_path = parsed.path + (("?" + parsed.query) if parsed.query else "")

    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=180)
    elif parsed.scheme == "http":
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=180)
    else:
        raise ValueError("Unsupported Gitea URL scheme: %s" % parsed.scheme)

    try:
        conn.putrequest("POST", request_path)
        conn.putheader("Authorization", "token %s" % cfg["GITEA_TOKEN"])
        conn.putheader("Accept", "application/json")
        conn.putheader("Content-Type", "multipart/form-data; boundary=%s" % boundary)
        conn.putheader("Content-Length", str(len(preamble) + size + len(ending)))
        conn.putheader("User-Agent", "github-to-gitea-mirror/1.0")
        conn.endheaders()
        conn.send(preamble)
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            conn.send(chunk)
        conn.send(ending)
        response = conn.getresponse()
        body = response.read()
        if response.status >= 400:
            raise urllib.error.HTTPError(
                url, response.status, response.reason, response.headers, io.BytesIO(body))
        return json.loads(body.decode("utf-8")) if body.strip() else None
    finally:
        conn.close()


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


def github_releases(cfg, repo_name):
    releases = []
    page = 1
    while True:
        url = ("https://api.github.com/repos/%s/%s/releases?per_page=100&page=%d"
               % (path_part(cfg["GITHUB_USER"]), path_part(repo_name), page))
        _, batch = api(url, token=cfg["GITHUB_TOKEN"], scheme="Bearer")
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return releases


def github_release_assets(cfg, repo_name, release_id):
    assets = []
    page = 1
    while True:
        url = ("https://api.github.com/repos/%s/%s/releases/%s/assets"
               "?per_page=100&page=%d"
               % (path_part(cfg["GITHUB_USER"]), path_part(repo_name), release_id, page))
        _, batch = api(url, token=cfg["GITHUB_TOKEN"], scheme="Bearer")
        if not batch:
            break
        assets.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return assets


def gitea_releases(cfg, repo_name):
    releases = []
    page = 1
    while True:
        url = ("%s/api/v1/repos/%s/%s/releases?limit=50&page=%d"
               % (cfg["GITEA_URL"], path_part(cfg["GITEA_OWNER"]),
                  path_part(repo_name), page))
        _, batch = api(url, token=cfg["GITEA_TOKEN"])
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return releases


def gitea_tags(cfg, repo_name):
    tags = set()
    page = 1
    while True:
        url = ("%s/api/v1/repos/%s/%s/tags?limit=50&page=%d"
               % (cfg["GITEA_URL"], path_part(cfg["GITEA_OWNER"]),
                  path_part(repo_name), page))
        _, batch = api(url, token=cfg["GITEA_TOKEN"])
        if not batch:
            break
        tags.update(tag["name"] for tag in batch)
        if len(batch) < 50:
            break
        page += 1
    return tags


def release_payload(release):
    return {
        "tag_name": release["tag_name"],
        "target_commitish": release.get("target_commitish") or "",
        "name": release.get("name") or "",
        "body": release.get("body") or "",
        "draft": bool(release.get("draft")),
        "prerelease": bool(release.get("prerelease")),
    }


def release_needs_update(source, target):
    """Return true for fields Gitea's PATCH endpoint can actually change."""
    wanted = release_payload(source)
    if bool(target.get("draft")) != wanted["draft"]:
        return True
    if bool(target.get("prerelease")) != wanted["prerelease"]:
        return True
    # Gitea ignores empty strings in release PATCH requests. Retaining old text
    # also follows this script's policy of never deleting backup information.
    if wanted["name"] and (target.get("name") or "") != wanted["name"]:
        return True
    if wanted["body"] and (target.get("body") or "") != wanted["body"]:
        return True
    return False


def release_update_payload(source):
    wanted = release_payload(source)
    payload = {
        "draft": wanted["draft"],
        "prerelease": wanted["prerelease"],
    }
    for key in ("target_commitish", "name", "body"):
        if wanted[key]:
            payload[key] = wanted[key]
    return payload


def sync_release_assets(cfg, repo_name, source_release, target_release, dry_run):
    copied = preserved = 0
    source_assets = github_release_assets(cfg, repo_name, source_release["id"])
    target_assets = {asset["name"]: asset for asset in target_release.get("assets", [])}

    for asset in source_assets:
        name = asset["name"]
        existing = target_assets.get(name)
        if existing is not None:
            source_size = int(asset.get("size") or 0)
            target_size = int(existing.get("size") or 0)
            if source_size and target_size and source_size != target_size:
                preserved += 1
                log("WARN", "%s release %s asset %s differs (%d vs %d bytes) - "
                    "Gitea copy left untouched"
                    % (repo_name, source_release["tag_name"], name,
                       source_size, target_size))
            continue

        if dry_run:
            log("DRY", "would copy %s release %s asset %s (%d bytes)"
                % (repo_name, source_release["tag_name"], name,
                   int(asset.get("size") or 0)))
            copied += 1
            continue

        tmp = None
        try:
            tmp, size = download_github_asset(cfg, asset)
            upload_release_asset(cfg, repo_name, target_release["id"], asset, tmp, size)
            copied += 1
            log("OK", "copied %s release %s asset %s (%d bytes)"
                % (repo_name, source_release["tag_name"], name, size))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            fail("%s release %s asset %s failed (HTTP %s): %s"
                 % (repo_name, source_release["tag_name"], name, exc.code, detail))
        except Exception as exc:
            fail("%s release %s asset %s failed: %s"
                 % (repo_name, source_release["tag_name"], name, exc))
        finally:
            if tmp is not None:
                tmp.close()
    return copied, preserved


def sync_releases(cfg, repo_name, dry_run):
    stats = {"created": 0, "updated": 0, "assets": 0, "preserved": 0, "waiting": 0}
    source_releases = github_releases(cfg, repo_name)
    target_releases = gitea_releases(cfg, repo_name)
    by_tag = {release["tag_name"]: release for release in target_releases}
    tags_to_check = [
        release["tag_name"] for release in source_releases
        if not release.get("draft")
        and (release["tag_name"] not in by_tag
             or by_tag[release["tag_name"]].get("draft"))
    ]
    available_tags = gitea_tags(cfg, repo_name) if tags_to_check else set()

    source_tags = {release["tag_name"] for release in source_releases}
    orphaned = sorted(tag for tag in by_tag if tag not in source_tags)
    if orphaned:
        log("WARN", "%s Gitea release(s) with no GitHub counterpart left untouched: %s"
            % (repo_name, ", ".join(orphaned)))

    for source in source_releases:
        tag = source["tag_name"]
        target = by_tag.get(tag)
        needs_mirrored_tag = (not source.get("draft")
                              and (target is None or target.get("draft")))
        if needs_mirrored_tag and tag not in available_tags:
            stats["waiting"] += 1
            log("WARN", "%s release %s waits for its tag to reach the Gitea mirror"
                % (repo_name, tag))
            continue

        try:
            if target is None:
                if dry_run:
                    target = {"id": source["id"], "tag_name": tag, "assets": []}
                    log("DRY", "would create %s release %s" % (repo_name, tag))
                else:
                    _, target = api(
                        "%s/api/v1/repos/%s/%s/releases"
                        % (cfg["GITEA_URL"], path_part(cfg["GITEA_OWNER"]),
                           path_part(repo_name)),
                        token=cfg["GITEA_TOKEN"], method="POST",
                        payload=release_payload(source))
                    log("OK", "created %s release %s" % (repo_name, tag))
                stats["created"] += 1
            elif release_needs_update(source, target):
                if dry_run:
                    log("DRY", "would update %s release %s" % (repo_name, tag))
                else:
                    _, target = api(
                        "%s/api/v1/repos/%s/%s/releases/%s"
                        % (cfg["GITEA_URL"], path_part(cfg["GITEA_OWNER"]),
                           path_part(repo_name), target["id"]),
                        token=cfg["GITEA_TOKEN"], method="PATCH",
                        payload=release_update_payload(source))
                    log("OK", "updated %s release %s" % (repo_name, tag))
                stats["updated"] += 1

            if truthy(cfg["SYNC_RELEASE_ASSETS"]):
                copied, preserved = sync_release_assets(
                    cfg, repo_name, source, target, dry_run)
                stats["assets"] += copied
                stats["preserved"] += preserved
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            fail("%s release %s failed (HTTP %s): %s"
                 % (repo_name, tag, exc.code, detail))
        except Exception as exc:
            fail("%s release %s failed: %s" % (repo_name, tag, exc))
    return stats


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
    release_totals = {"created": 0, "updated": 0, "assets": 0,
                      "preserved": 0, "waiting": 0}
    for repo in sorted(repos, key=lambda r: r["name"].lower()):
        name = repo["name"]
        try:
            existing = gitea_repo(cfg, name)
        except Exception as exc:
            fail("%s: cannot query Gitea: %s" % (name, exc))
            continue

        if existing is None:
            mirror_created = False
            try:
                create_mirror(cfg, repo, dry_run)
                created += 1
                mirror_created = True
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                fail("%s: migrate failed (HTTP %s): %s" % (name, exc.code, detail))
            except Exception as exc:
                fail("%s: migrate failed: %s" % (name, exc))

            if mirror_created and truthy(cfg["SYNC_RELEASES"]):
                try:
                    if dry_run:
                        count = len(github_releases(cfg, name))
                        if count:
                            log("DRY", "would synchronize %d release(s) after creating %s"
                                % (count, name))
                    else:
                        stats = sync_releases(cfg, name, dry_run)
                        for key in release_totals:
                            release_totals[key] += stats[key]
                except Exception as exc:
                    fail("%s: could not synchronize releases: %s" % (name, exc))
            continue

        skipped += 1
        if not existing.get("mirror"):
            log("WARN", "%s exists in Gitea but is not a mirror - left untouched" % name)
            continue
        try:
            align_interval(cfg, name, existing, dry_run)
        except Exception as exc:
            fail("%s: could not update mirror interval: %s" % (name, exc))

        if truthy(cfg["SYNC_RELEASES"]):
            try:
                stats = sync_releases(cfg, name, dry_run)
                for key in release_totals:
                    release_totals[key] += stats[key]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                fail("%s: release sync failed (HTTP %s): %s"
                     % (name, exc.code, detail))
            except Exception as exc:
                fail("%s: release sync failed: %s" % (name, exc))

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

    log("INFO", "Done: %d mirror(s) created, %d already present; releases: "
        "%d created, %d updated, %d asset(s) copied, %d preserved, "
        "%d waiting for tags; %d error(s)"
        % (created, skipped, release_totals["created"], release_totals["updated"],
           release_totals["assets"], release_totals["preserved"],
           release_totals["waiting"], errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
