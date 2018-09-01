import asyncio
import os

import pytest

from .i3mock import I3Mock
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
    def __init__(self):
        self.mock = None
        self.conn = None
        self._mock_reader_fobj = None
        self._mock_writer_fobj = None
        self._conn_reader_fobj = None
        self._conn_writer_fobj = None
        self._mock_run_task = None

    async def setup(self, loop):
        mock_reader, mock_reader_fobj, conn_writer, conn_writer_fobj = (
                await stream_pipe(loop))
        conn_reader, conn_reader_fobj, mock_writer, mock_writer_fobj = (
                await stream_pipe(loop))
        self._mock_reader_fobj = mock_reader_fobj
        self._mock_writer_fobj = mock_writer_fobj
        self._conn_reader_fobj = conn_reader_fobj
        self._conn_writer_fobj = conn_writer_fobj
        self.mock = I3Mock(loop, mock_reader, mock_writer)
        self.conn = I3Connection(loop, conn_reader, conn_writer)
        self._mock_run_task = asyncio.ensure_future(self.mock.run())

    async def teardown(self, loop):
        self.mock._close()
        await self._mock_run_task
        # ensure all pipe file descriptors are closed
        self._mock_reader_fobj.close()
        self._mock_writer_fobj.close()
        self._conn_reader_fobj.close()
        self._conn_writer_fobj.close()


@pytest.fixture
def i3(event_loop):
    i3 = I3()
    event_loop.run_until_complete(i3.setup(event_loop))
    yield i3
    event_loop.run_until_complete(i3.teardown(event_loop))
 

@pytest.fixture
def i3mock(i3):
    return i3.mock


@pytest.fixture
def i3conn(i3):
    return i3.conn
