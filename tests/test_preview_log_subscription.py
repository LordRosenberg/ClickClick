"""Preview reuses the existing bounded bus; slow logs never block producers."""
from control_api.sse import EventBus


def test_slow_log_subscriber_drops_its_own_events_without_blocking_others():
    bus = EventBus()
    slow, close_slow = bus.subscribe("T")
    fast, close_fast = bus.subscribe("T")
    try:
        for sequence in range(2000):
            event = {"kind": "agent_llm_stream", "payload": {"sequence": sequence, "text": "sample"}}
            bus.publish("T", event)
            assert fast.get_nowait() is event
            assert event["payload"]["sequence"] == sequence
        assert slow.qsize() == slow.maxsize == 256
        assert slow.get_nowait()["payload"]["sequence"] == 0
        assert bus.subscriber_count("T") == 2
    finally:
        close_slow()
        close_fast()
    assert bus.subscriber_count("T") == 0
