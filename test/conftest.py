import asyncio
import os

import pytest

from .mock import I3Mock, I3BarMock, i3msg
from .plugin import Plugin
from ..i3hub import I3Connection, I3Hub


async def stream_pipe(loop):
    r, w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    # Wrap the read end of the pipe into a StreamReader
    reader_fobj = os.fdopen(r, 'rb', 0)
    reader = asyncio.StreamReader(loop=loop)
    reader_proto = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_proto, reader_fobj)
    # Wrap the write end of the pipe into a StreamWriter
    writer_fobj = os.fdopen(w, 'wb', 0)
    writer_trans, writer_proto = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, writer_fobj)
    writer = asyncio.streams.StreamWriter(writer_trans, writer_proto, None,
            loop)
    return reader, reader_fobj, writer, writer_fobj


class I3(object):
    def __init__(self, plugins, run_i3hub):
        self.mock = None
        self.conn = None
        self.barmock = None
        self.hub = None
        self._mreader_fobj = None
        self._mwriter_fobj = None
        self._creader_fobj = None
        self._cwriter_fobj = None
        self._breader_fobj = None
        self._bwriter_fobj = None
        self._hreader_fobj = None
        self._hwriter_fobj = None
        self._all_run_task = None
        self._plugins = plugins
        self._run_i3hub = run_i3hub

    async def setup(self, loop):
        # 2 pipes for communication between I3Connection and I3Mock
        mreader, self._mreader_fobj, cwriter, self._cwriter_fobj = (
                await stream_pipe(loop))
        creader, self._creader_fobj, mwriter, self._mwriter_fobj = (
                await stream_pipe(loop))
        self.mock = I3Mock(loop, mreader, mwriter)
        self.conn = I3Connection(loop, creader, cwriter)
        # and 2 more pipes for communication between I3Hub and I3BarMock
        breader, self._breader_fobj, hwriter, self._hwriter_fobj = (
                await stream_pipe(loop))
        hreader, self._hreader_fobj, bwriter, self._bwriter_fobj = (
                await stream_pipe(loop))
        self.barmock = I3BarMock(loop, breader, bwriter)
        self.hub = I3Hub(loop, self.conn, hreader, hwriter, self._plugins,
                status_command=None)
        tasks = [self.barmock.run(), self.mock.run()]
        if self._run_i3hub:
            # tell I3Mock to expect and reply to a subscribe request from I3Hub
            self.mock.expect_request(
                    i3msg(2, '["window","shutdown"]'),
                    i3msg(2, '{"success":true}'))
            # use a future to wait until i3hub has fully completed the setup
            ready = asyncio.Future(loop=loop)
            tasks.append(self.hub.run(ready))
        self._all_run_task = asyncio.ensure_future(asyncio.gather(*tasks))
        if self._run_i3hub:
            await ready

    async def teardown(self, loop):
        self.mock.close()
        self.barmock.close()
        await self._all_run_task
        # ensure all pipe file descriptors are closed
        self._mreader_fobj.close()
        self._mwriter_fobj.close()
        self._creader_fobj.close()
        self._cwriter_fobj.close()
        self._breader_fobj.close()
        self._bwriter_fobj.close()
        self._hreader_fobj.close()
        self._hwriter_fobj.close()


@pytest.fixture
def events():
    return []


@pytest.fixture
def plugins(events):
    return [Plugin(events)]


@pytest.fixture
def i3(request, event_loop, plugins):
    run_i3hub = getattr(request.module, 'run_i3hub', False)
    i3 = I3(plugins, run_i3hub)
    event_loop.run_until_complete(i3.setup(event_loop))
    yield i3
    event_loop.run_until_complete(i3.teardown(event_loop))
 

@pytest.fixture
def i3mock(i3):
    return i3.mock


@pytest.fixture
def i3conn(i3):
    return i3.conn


@pytest.fixture
def i3barmock(i3):
    return i3.barmock


@pytest.fixture
def i3hub(i3):
    return i3.hub


@pytest.fixture
def i3api(i3hub):
    return i3hub._i3api
