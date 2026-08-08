import io
import unittest
from unittest import mock

import sync_mirrors as mirror


def config(**overrides):
    values = {
        "GITHUB_USER": "octocat",
        "GITHUB_TOKEN": "github-token",
        "GITEA_URL": "http://gitea.invalid",
        "GITEA_TOKEN": "gitea-token",
        "GITEA_OWNER": "backup",
        "SYNC_RELEASE_ASSETS": "true",
    }
    values.update(overrides)
    return values


def github_release(**overrides):
    values = {
        "id": 101,
        "tag_name": "v1.0.0",
        "target_commitish": "main",
        "name": "Version 1.0.0",
        "body": "Release notes",
        "draft": False,
        "prerelease": False,
    }
    values.update(overrides)
    return values


class ReleaseSyncTests(unittest.TestCase):
    @mock.patch.object(mirror, "upload_release_asset")
    @mock.patch.object(mirror, "download_github_asset")
    @mock.patch.object(mirror, "github_release_assets")
    @mock.patch.object(mirror, "gitea_tags")
    @mock.patch.object(mirror, "gitea_releases")
    @mock.patch.object(mirror, "github_releases")
    @mock.patch.object(mirror, "api")
    def test_creates_release_and_copies_missing_asset(
            self, api, github_releases, gitea_releases, gitea_tags,
            github_assets, download_asset, upload_asset):
        source = github_release()
        asset = {
            "id": 501,
            "name": "program.zip",
            "size": 3,
            "url": "https://api.github.com/assets/501",
            "content_type": "application/zip",
        }
        github_releases.return_value = [source]
        gitea_releases.return_value = []
        gitea_tags.return_value = {"v1.0.0"}
        github_assets.return_value = [asset]
        download_asset.return_value = (io.BytesIO(b"zip"), 3)
        api.return_value = mirror.Response(
            201, {"id": 901, "tag_name": "v1.0.0", "assets": []}, {})

        stats = mirror.sync_releases(config(), "project", False)

        self.assertEqual(1, stats["created"])
        self.assertEqual(1, stats["assets"])
        self.assertEqual(0, stats["errors"])
        self.assertEqual("POST", api.call_args.kwargs["method"])
        self.assertEqual("v1.0.0", api.call_args.kwargs["payload"]["tag_name"])
        upload_asset.assert_called_once()

    @mock.patch.object(mirror, "github_release_assets")
    @mock.patch.object(mirror, "gitea_tags")
    @mock.patch.object(mirror, "gitea_releases")
    @mock.patch.object(mirror, "github_releases")
    @mock.patch.object(mirror, "api")
    def test_second_run_does_not_write_matching_release(
            self, api, github_releases, gitea_releases, gitea_tags, github_assets):
        source = github_release()
        target = {
            "id": 901,
            "tag_name": "v1.0.0",
            "name": "Version 1.0.0",
            "body": "Release notes",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": "program.zip", "size": 3}],
        }
        github_releases.return_value = [source]
        gitea_releases.return_value = [target]
        github_assets.return_value = [{"name": "program.zip", "size": 3}]

        stats = mirror.sync_releases(config(), "project", False)

        self.assertEqual(0, stats["created"])
        self.assertEqual(0, stats["updated"])
        self.assertEqual(0, stats["assets"])
        api.assert_not_called()
        gitea_tags.assert_not_called()

    @mock.patch.object(mirror, "gitea_tags", return_value=set())
    @mock.patch.object(mirror, "gitea_releases", return_value=[])
    @mock.patch.object(mirror, "github_releases", return_value=[github_release()])
    @mock.patch.object(mirror, "api")
    def test_waits_when_mirror_has_not_fetched_release_tag(
            self, api, github_releases, gitea_releases, gitea_tags):
        stats = mirror.sync_releases(config(), "project", False)

        self.assertEqual(1, stats["waiting"])
        self.assertEqual(0, stats["created"])
        api.assert_not_called()

    @mock.patch.object(mirror, "gitea_tags")
    @mock.patch.object(mirror, "gitea_releases", return_value=[])
    @mock.patch.object(
        mirror, "github_releases", return_value=[github_release(draft=True)])
    @mock.patch.object(mirror, "api")
    def test_creates_draft_without_requiring_mirrored_tag(
            self, api, github_releases, gitea_releases, gitea_tags):
        api.return_value = mirror.Response(
            201,
            {"id": 901, "tag_name": "v1.0.0", "draft": True, "assets": []},
            {},
        )

        stats = mirror.sync_releases(
            config(SYNC_RELEASE_ASSETS="false"), "project", False)

        self.assertEqual(1, stats["created"])
        self.assertEqual(0, stats["waiting"])
        gitea_tags.assert_not_called()
        self.assertTrue(api.call_args.kwargs["payload"]["draft"])

    @mock.patch.object(mirror, "gitea_releases")
    @mock.patch.object(mirror, "github_releases")
    @mock.patch.object(mirror, "api")
    def test_updates_metadata_without_touching_assets(
            self, api, github_releases, gitea_releases):
        source = github_release(body="New notes", prerelease=True)
        target = {
            "id": 901,
            "tag_name": "v1.0.0",
            "name": "Version 1.0.0",
            "body": "Old notes",
            "draft": False,
            "prerelease": False,
            "assets": [],
        }
        github_releases.return_value = [source]
        gitea_releases.return_value = [target]
        api.return_value = mirror.Response(
            200, dict(target, body="New notes", prerelease=True), {})

        stats = mirror.sync_releases(
            config(SYNC_RELEASE_ASSETS="false"), "project", False)

        self.assertEqual(1, stats["updated"])
        self.assertEqual("PATCH", api.call_args.kwargs["method"])
        self.assertEqual("New notes", api.call_args.kwargs["payload"]["body"])
        self.assertTrue(api.call_args.kwargs["payload"]["prerelease"])

    @mock.patch.object(mirror, "download_github_asset")
    @mock.patch.object(mirror, "github_release_assets")
    @mock.patch.object(mirror, "gitea_releases")
    @mock.patch.object(mirror, "github_releases")
    def test_keeps_changed_asset_in_gitea(
            self, github_releases, gitea_releases, github_assets, download_asset):
        source = github_release()
        target = {
            "id": 901,
            "tag_name": "v1.0.0",
            "name": "Version 1.0.0",
            "body": "Release notes",
            "draft": False,
            "prerelease": False,
            "assets": [{"name": "program.zip", "size": 2}],
        }
        github_releases.return_value = [source]
        gitea_releases.return_value = [target]
        github_assets.return_value = [{"name": "program.zip", "size": 3}]

        stats = mirror.sync_releases(config(), "project", False)

        self.assertEqual(1, stats["preserved"])
        download_asset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
