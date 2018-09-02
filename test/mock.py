import asyncio
import struct


def i3msg(msg_type, msg_payload):
    body = msg_payload.encode('utf-8')
    header = b'i3-ipc' + struct.pack('=II', len(body), msg_type)
    return header + body


def i3event(msg_type, msg_payload):
    body = msg_payload.encode('utf-8')
    header = b'i3-ipc' + struct.pack('=II', len(body), msg_type | 0x80000000)
    return header + body


class Mock(object):
    def __init__(self, loop, reader, writer):
        self._loop = loop
        self._reader = reader
        self._writer = writer
        self._test_action_future = None
        self._expected_data = None
        self._actual_data = None
        self._closed = False

    def _set_test_action_result(self, result):
        if not self._test_action_future:
            self._test_action_future = asyncio.Future(loop=self._loop)
        self._test_action_future.set_result(result)

    def _test_action(self):
        if not self._test_action_future:
            self._test_action_future = asyncio.Future(loop=self._loop)
        return self._test_action_future

    def verify(self):
        assert self._expected_data == self._actual_data
        self._expectation = None

    def close(self):
        if not self._closed:
            self._set_test_action_result('close')
            self._closed = True

    async def run(self):
        while True:
            action = await self._test_action()
            self._test_action_future = None
            if action == 'close':
                break
            await self._handle_test_action(action)
        self._writer.close()


class I3Mock(Mock):
    def expect_request(self, request_data, reply):
        self._set_test_action_result(('request', request_data, reply))

    def send_event(self, event_data):
        self._set_test_action_result(('event', event_data))

    async def _handle_test_action(self, action):
        if action[0] == 'request':
            _, data, reply = action
            # save both expected and actual for verification later
            self._actual_data = await self._reader.readexactly(len(data))
            self._expected_data = data
            self._writer.write(reply)
        else:
            assert action[0] == 'event'
            _, data = action
            self._writer.write(data)


class I3BarMock(Mock):
    def expect_update(self, update_data):
        self._test_action_future.set_result(('update', update_data))

    def send_click(self, click_data):
        self._test_action_future.set_result(('click', click_data))

    async def _handle_test_action(self, action):
        if action[0] == 'update':
            _, data = action
            self._actual_data = await self._reader.readexactly(len(data))
            self._expected_data = data
        else:
            assert action[0] == 'click'
            _, data = action
            self._writer.write(data)
