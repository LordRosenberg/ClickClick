import hashlib

import httpx
import pytest

from driver import adb, collector_release as release
from driver.environment import resolve_collector_apk_path


def test_local_build_precedes_release_cache_and_explicit_path_precedes_build(tmp_path):
    output = tmp_path / "android/accessibility-collector/app/build/outputs/apk/debug"
    output.mkdir(parents=True)
    built = output / "app-debug.apk"
    built.write_bytes(b"new local build")
    (output / "output-metadata.json").write_text(
        '{"elements":[{"versionName":"' + release.COLLECTOR_VERSION + '"}]}', encoding="utf-8",
    )
    cache = release.collector_cache_path(root=tmp_path)
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"older release")
    assert resolve_collector_apk_path(root=tmp_path) == str(built)
    assert resolve_collector_apk_path("explicit.apk", root=tmp_path) == "explicit.apk"


@pytest.mark.asyncio
async def test_release_verified_cache_needs_no_network(tmp_path, monkeypatch):
    cache = tmp_path / "collector.apk"
    cache.write_bytes(b"verified")
    monkeypatch.setattr(release, "COLLECTOR_SHA256", hashlib.sha256(b"verified").hexdigest())
    def forbidden(**kwargs):
        pytest.fail("Verified cache should not access the network")
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert await release.download_collector_release(cache) == cache


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "hash", "missing", "unpublished"])
async def test_release_download_is_pinned_and_atomic(tmp_path, monkeypatch, failure):
    cache = tmp_path / "collector.apk"
    cache.write_bytes(b"corrupt cache")
    monkeypatch.setattr(release, "COLLECTOR_SHA256", hashlib.sha256(b"valid apk").hexdigest())
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("CLICKCLICK_COLLECTOR_RELEASE_REPO", "test/repo")
    requests = []
    def respond(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-token"
        if len(requests) == 1:
            assert str(request.url).endswith("/releases/tags/" + release.COLLECTOR_TAG)
            return httpx.Response(404 if failure == "unpublished" else 200, json={
                "assets": [] if failure == "missing" else [{"name": release.COLLECTOR_ASSET, "id": 42}],
            })
        assert str(request.url).endswith("/releases/assets/42")
        assert request.headers["Accept"] == "application/octet-stream"
        return httpx.Response(200, content=b"wrong" if failure == "hash" else b"valid apk")
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    if failure:
        with pytest.raises(RuntimeError):
            await release.download_collector_release(cache)
        assert cache.read_bytes() == b"corrupt cache"
    else:
        assert await release.download_collector_release(cache) == cache
        assert cache.read_bytes() == b"valid apk"
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.asyncio
@pytest.mark.parametrize("digest", ["a" * 64, "invalid", ""])
async def test_installed_apk_digest(tmp_path, monkeypatch, digest):
    calls = []
    async def shell(serial, args, **kwargs):
        calls.append(args)
        return b"package:/data/app/test/base.apk\n" if len(calls) == 1 else digest.encode()
    monkeypatch.setattr(adb, "shell_async", shell)
    assert await adb.package_apk_sha256_async("S", "test") == (digest if len(digest) == 64 else None)
    assert calls == [["pm", "path", "test"], ["sha256sum", "/data/app/test/base.apk"]]
