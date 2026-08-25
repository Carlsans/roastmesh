from __future__ import annotations

import queue

from roastnet.gui import single_instance

# Dedicated test port range -- distinct from single_instance.PORT (the
# real one) so these tests never collide with an actual running instance.
BASE_PORT = 41999


def test_another_instance_is_running_is_false_when_nothing_listens() -> None:
    assert single_instance.another_instance_is_running(port=BASE_PORT + 1, timeout=0.2) is False


def test_start_focus_listener_then_another_instance_is_running_is_true_and_callback_fires() -> None:
    events: queue.Queue = queue.Queue()
    listener = single_instance.start_focus_listener(lambda: events.put(None), port=BASE_PORT + 2)
    try:
        assert listener is not None
        assert single_instance.another_instance_is_running(port=BASE_PORT + 2, timeout=2.0) is True
        events.get(timeout=2.0)
    finally:
        listener.close()


def test_second_listener_on_the_same_port_fails_to_bind_and_returns_none() -> None:
    first = single_instance.start_focus_listener(lambda: None, port=BASE_PORT + 3)
    try:
        assert first is not None
        second = single_instance.start_focus_listener(lambda: None, port=BASE_PORT + 3)
        assert second is None
    finally:
        first.close()


def test_multiple_focus_requests_each_trigger_the_callback() -> None:
    events: queue.Queue = queue.Queue()
    listener = single_instance.start_focus_listener(lambda: events.put(None), port=BASE_PORT + 4)
    try:
        for _ in range(3):
            assert single_instance.another_instance_is_running(port=BASE_PORT + 4, timeout=2.0) is True
        for _ in range(3):
            events.get(timeout=2.0)
    finally:
        listener.close()
