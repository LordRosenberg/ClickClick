"""Capture readiness must agree with runtime image-only/fallback capabilities."""
import asyncio
from io import BytesIO
import json

from PIL import Image
import pytest

from evaluation.androidworld.agent_worker import capture_preflight, ObservationPreflightError


def pixels():
    stream = BytesIO()
    Image.new('RGB', (108, 240), 'blue').save(stream, format='PNG')
    return stream.getvalue()


class Driver:
    def __init__(self, frames, warmup_error=None):
        self.frames = iter(frames)
        self.calls = 0
        self.warmup_error = warmup_error

    async def warm_observation_provider(self):
        if self.warmup_error:
            raise self.warmup_error
        return True

    async def get_frame(self):
        self.calls += 1
        item = next(self.frames)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['scrcpy', 'adb_screencap'])
async def test_valid_pixels_without_tree_pass_and_keep_fallback_diagnostics(tmp_path, provider):
    capture = {'pixel_provider': provider, 'complete': False,
               'tree_providers_exhausted': True, 'pixel_failure': 'no_frame',
               'fallback_edges': [{'from': 'scrcpy', 'to': 'adb_screencap', 'reason': 'no_frame'}]}
    driver = Driver([({'_capture': capture}, pixels())])
    report = await capture_preflight(driver, tmp_path)
    assert report['accepted'] and driver.calls == 1
    saved = json.loads((tmp_path/'capture-preflight.json').read_text())
    assert saved['capture'] == capture
    assert saved['decoded_size'] == [108, 240]


@pytest.mark.asyncio
async def test_stream_warmup_failure_does_not_block_adb_fallback(tmp_path):
    driver = Driver([({'_capture': {'pixel_provider': 'adb_screencap'}}, pixels())],
                    warmup_error=RuntimeError('decoder startup'))
    report = await capture_preflight(driver, tmp_path)
    assert report['accepted']
    assert report['warmup_error'] == 'RuntimeError: decoder startup'


@pytest.mark.asyncio
async def test_corrupt_image_gets_one_fresh_retry_not_metadata_acceptance(tmp_path):
    capture = {'pixel_provider': 'scrcpy', 'image_geometry': [1080, 2400], 'complete': True}
    driver = Driver([({'_capture': capture}, b'invalid PNG'),
                     ({'_capture': capture}, pixels())])
    report = await capture_preflight(driver, tmp_path)
    assert driver.calls == 2 and report['accepted']
    assert [a['accepted'] for a in report['attempts']] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize('frame', [
    ({'_capture': {'pixel_provider': 'scrcpy'}}, b''),
    ({'_capture': {'pixel_provider': 'cached_preview'}}, pixels()),
    RuntimeError('device unavailable'),
])
async def test_unusable_capture_stops_after_two_attempts_with_diagnostics(tmp_path, frame):
    driver = Driver([frame, frame])
    with pytest.raises(ObservationPreflightError):
        await capture_preflight(driver, tmp_path)
    report = json.loads((tmp_path/'capture-preflight.json').read_text())
    assert driver.calls == 2 and not report['accepted']
    assert len(report['attempts']) == 2


@pytest.mark.asyncio
async def test_capture_deadline_and_cancellation_remain_bounded(tmp_path):
    class HangingDriver(Driver):
        async def get_frame(self):
            await asyncio.Event().wait()
    with pytest.raises(ObservationPreflightError):
        await capture_preflight(HangingDriver([]), tmp_path, timeout_s=.01)
    report = json.loads((tmp_path/'capture-preflight.json').read_text())
    assert report['error'] == 'observation preflight deadline exceeded'

    task = asyncio.create_task(capture_preflight(HangingDriver([]), tmp_path))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
