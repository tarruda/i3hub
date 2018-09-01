import asyncio
import json
import struct

import pytest


pytestmark = pytest.mark.asyncio


def i3msg(msg_type, msg_payload):
    body = msg_payload.encode('utf-8')
    header = b'i3-ipc' + struct.pack('=II', len(body), msg_type)
    return header + body

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
    i3mock.expect(i3msg(type, payload), i3msg(type, reply))
    assert json.loads(reply) == await call_method(i3conn)
    i3mock.verify()
