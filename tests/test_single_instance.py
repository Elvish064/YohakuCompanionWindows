from __future__ import annotations

from uuid import uuid4

from yohaku_companion_windows.single_instance import SingleInstance


def test_second_instance_only_activates_first(qtbot) -> None:  # type: ignore[no-untyped-def]
    activated: list[bool] = []
    name = f"yohaku-test-{uuid4()}"
    first = SingleInstance(name, lambda: activated.append(True))
    second = SingleInstance(name, lambda: None)
    try:
        assert first.acquire()
        assert not second.acquire()
        qtbot.waitUntil(lambda: activated == [True], timeout=1000)
    finally:
        second.close()
        first.close()
