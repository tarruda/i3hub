import asyncio
import pytest

from .mock import i3event

pytestmark = pytest.mark.asyncio
run_i3hub = True

async def test_init_event(i3api, events):
    assert events == [(i3api, 'i3hub::init', None)]


async def test_i3_events(i3api, i3mock, events):
    i3mock.send_event(i3event(3, '[1]'))
    await asyncio.sleep(0.001)
    i3mock.send_event(i3event(3, '[2]'))
    await asyncio.sleep(0.001)
    i3mock.send_event(i3event(3, '[3]'))
    await asyncio.sleep(0.001)
    assert events[1] == (i3api, 'i3::window', [1])
    assert events[2] == (i3api, 'i3::window', [2])
    assert events[3] == (i3api, 'i3::window', [3])


async def test_shutdown_event_closes_i3hub(i3api, i3mock, i3hub, events):
    i3mock.send_event(i3event(6, '[1,2]'))
    await asyncio.sleep(0.001)
    assert events[1] == (i3api, 'i3::shutdown', [1, 2])
    # new events are ignored
    i3mock.send_event(i3event(3, '[1]'))
    await asyncio.sleep(0.001)
    assert len(events) == 2
    with pytest.raises(Exception,
            message='This I3Hub instance was already closed'):
        await i3hub.run()


async def test_shutdown_through_eof_event(i3api, i3mock, events):
    i3mock.close()
    await asyncio.sleep(0.001)
    assert events[1] == (i3api, 'i3::shutdown', None)
