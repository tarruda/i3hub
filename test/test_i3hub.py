import asyncio
import pytest

from .util import i3event, spin

pytestmark = pytest.mark.asyncio
run_i3hub = True


async def test_module_init_event(i3api, moduleevents):
    assert moduleevents == [(i3api, 'i3hub::init', {
        'running_as_status': True,
        'config': {}
        })]


async def test_init_event(i3api, i3events):
    assert i3events == [(i3api, 'i3hub::init', {
        'running_as_status': True,
        'config': {}
        })]


async def test_module_i3_events(i3api, i3mock, moduleevents):
    i3mock.send_event(i3event(3, '[1]'))
    await spin()
    i3mock.send_event(i3event(3, '[2]'))
    await spin()
    i3mock.send_event(i3event(3, '[3]'))
    await spin()
    assert moduleevents[1] == (i3api, 'i3::window', [1])
    assert moduleevents[2] == (i3api, 'i3::window', [2])
    assert moduleevents[3] == (i3api, 'i3::window', [3])


async def test_i3_events(i3api, i3mock, i3events):
    i3mock.send_event(i3event(3, '[1]'))
    await spin()
    i3mock.send_event(i3event(3, '[2]'))
    await spin()
    i3mock.send_event(i3event(3, '[3]'))
    await spin()
    assert i3events[1] == (i3api, 'i3::window', [1])
    assert i3events[2] == (i3api, 'i3::window', [2])
    assert i3events[3] == (i3api, 'i3::window', [3])


async def test_shutdown_event_closes_i3hub(i3api, i3mock, i3hub, i3events):
    i3mock.send_event(i3event(6, '[1,2]'))
    await spin()
    assert i3events[1] == (i3api, 'i3::shutdown', [1, 2])
    # new events are ignored
    i3mock.send_event(i3event(3, '[1]'))
    await spin()
    assert len(i3events) == 2
    with pytest.raises(Exception,
            message='This I3Hub instance was already closed'):
        await i3hub.run()


async def test_shutdown_through_closed_connection(i3api, i3mock, i3events):
    i3mock.close()
    await spin()
    assert i3events[1] == (i3api, 'i3::shutdown', 'eof')


async def test_i3bar_initial_data(i3barmock):
    initial_data = (
            b'{"click_events":true,"cont_signal":63,'
            b'"stop_signal":64,"version":1}\n[\n[1]\n')
    i3barmock.expect_update(initial_data)
    await spin()
    i3barmock.verify()


async def test_get_and_update_status(i3barmock, i3api):
    await test_i3bar_initial_data(i3barmock)
    i3api.refresh_i3bar()
    i3barmock.expect_update(b',[{"1":2,"3":"4"}]\n')
    await spin()
    i3barmock.verify()
    i3api.refresh_i3bar()
    i3barmock.expect_update(b',[{"1":2,"3":"4"},2]\n')
    await spin()
    i3barmock.verify()
    i3api.refresh_i3bar()
    i3barmock.expect_update(b',[{"1":2,"3":"4"}]\n')
    await spin()
    i3barmock.verify()


async def test_i3bar_click_event(i3barmock, i3api, statusevents):
    i3barmock.send_click(b'[\n[1,2,3]\n')
    await spin()
    assert len(statusevents) == 1
    assert statusevents[0] == (i3api, 'i3hub::i3bar_click', [1,2,3])
    i3barmock.send_click(b',["click!"]\n')
    await spin()
    assert statusevents[1] == (i3api, 'i3hub::i3bar_click', ['click!'])
    i3barmock.send_click(b',[""]\n')
    await spin()
    assert statusevents[2] == (i3api, 'i3hub::i3bar_click', [''])


async def test_i3bar_stop_cont_events(i3hub, i3api, statusevents):
    await i3hub.dispatch_stop()
    await spin()
    assert statusevents[0] == (i3api, 'i3hub::i3bar_suspend', None)
    await i3hub.dispatch_cont()
    await spin()
    assert statusevents[1] == (i3api, 'i3hub::i3bar_resume', None)


async def test_extension_event(i3api, extensionevents):
    assert extensionevents == []
    arg = []
    await i3api.emit_event('some_extension::custom', arg)
    assert extensionevents[0] == (i3api, 'extension::some_extension::custom',
            arg)
    assert arg == ['extension-data']
