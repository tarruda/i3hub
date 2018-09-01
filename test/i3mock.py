import asyncio
import os
import struct
class I3Mock(object):
    def __init__(self, loop, reader, writer):
        self._loop = loop
        self._reader = reader
        self._writer = writer
        self._expectation = None
        self._expectation_set_future = None
        self._expected_data = None
        self._actual_data = None

    def verify(self):
        assert self._expected_data == self._actual_data
        self._expectation = None

    def expect(self, data, reply):
        if self._expectation is not None:
            raise Exception('verify expectation before calling expect again')
        if self._expectation_set_future is None:
            raise Exception('not waiting for expectation yet')
        self._expectation = (data, reply)
        self._expectation_set_future.set_result(None)

    def _expectation_set(self):
        self._expectation_set_future = asyncio.Future(loop=self._loop)
        return self._expectation_set_future

    def _close(self):
        self._expectation = (None, None)
        self._expectation_set_future.set_result(None)

    async def run(self):
        while True:
            await self._expectation_set()
            data, reply = self._expectation
            if data is None:
                break
            self._actual_data = await self._reader.readexactly(len(data))
            self._expected_data = data
            self._writer.write(reply)
