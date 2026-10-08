from scripts.project_stats import sample, chart


def test_only_installer_assets_are_counted_and_same_day_samples_replace():
    release = {"assets": [
        {"id": 1, "name": "ClickClick-0.1.0-windows-x86_64.exe", "download_count": 4},
        {"id": 2, "name": "ClickClick-0.1.0-macos-aarch64.zip", "download_count": 2},
        {"id": 3, "name": "clickclick-collector-0.4.5-debug.apk", "download_count": 100},
        {"id": 4, "name": "ClickClick-0.1.0-windows-x86_64.sha256", "download_count": 50},
    ]}
    first = sample({}, [release], "2026-10-08")
    assert first["daily"] == [{"date": "2026-10-08", "downloads": 6}]
    release["assets"][0]["download_count"] = 7
    again = sample(first, [release], "2026-10-08")
    assert again["daily"] == [{"date": "2026-10-08", "downloads": 9}]
    assert first["daily"][0]["downloads"] == 6
    missing = sample(again, [], "2026-10-09")
    assert missing["daily"][-1]["downloads"] == 9
    assert "2026-10-08" in chart(missing)
