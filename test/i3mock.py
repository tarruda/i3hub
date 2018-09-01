import asyncio
import os
import struct


class I3Mock(object):
    def __init__(self, loop, reader, writer):
        self._loop = loop
        self._reader = reader
        self._writer = writer
        self._test_action_future = None
        self._expected_data = None
        self._actual_data = None

    def verify(self):
        assert self._expected_data == self._actual_data
        self._expectation = None

    def expect_request(self, request_data, reply):
        self._test_action_future.set_result(('request', request_data, reply))

    def send_event(self, event_data):
        self._test_action_future.set_result(('event', event_data))

    def _test_action(self):
        self._test_action_future = asyncio.Future(loop=self._loop)
        return self._test_action_future

    def _close(self):
        self._test_action_future.set_result('close')

    async def run(self):
        while True:
            action = await self._test_action()
            if action == 'close':
                break
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
