import asyncio
import json

import pytest

from .util import i3msg, i3event

pytestmark = pytest.mark.asyncio


msg_tests = [
    (lambda c: c.command('border normal'), 0, 
        'border normal', '[{"success":true}]'),
    (lambda c: c.get_workspaces(), 1, '', '[]'),
    (lambda c: c.subscribe(['window', 'output', 'binding']), 2,
        '["window","output","binding"]', '{"success":true}'),
    (lambda c: c.get_outputs(), 3, '', '[]'),
    (lambda c: c.get_tree(), 4, '', '[]'),
    (lambda c: c.get_marks(), 5, '', '[]'),
    (lambda c: c.get_bar_config(), 6, '', '[]'),
    (lambda c: c.get_version(), 7, '', '{}'),
    (lambda c: c.get_binding_modes(), 8, '', '[]'),
    (lambda c: c.get_config(), 9, '', '[]'),
    (lambda c: c.send_tick(), 10, '', '[]'),
]

@pytest.mark.parametrize('call_method,type,payload,reply', msg_tests)
async def test_messages(i3mock, i3conn, call_method, type, payload, reply):
    i3mock.expect_request(i3msg(type, payload), i3msg(type, reply))
    assert json.loads(reply) == await call_method(i3conn)
    i3mock.verify()


event_tests = [
    (i3event(0, '{}'), ('workspace', {})),
    (i3event(1, '{}'), ('output', {})),
    (i3event(2, '{}'), ('mode', {})),
    (i3event(3, '{}'), ('window', {})),
    (i3event(4, '{}'), ('barconfig_update', {})),
    (i3event(5, '{}'), ('binding', {})),
    (i3event(6, '{}'), ('shutdown', {})),
    (i3event(7, '{}'), ('tick', {})),
]


@pytest.mark.parametrize('event_payload,result', event_tests)
async def test_events(i3mock, i3conn, event_payload, result):
    i3mock.send_event(event_payload)
    assert result == await i3conn.wait_event()
