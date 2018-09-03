import asyncio
import os
import struct


try:
    DEFAULT_SPIN_COUNT = float(os.getenv('I3HUB_TEST_SPIN_COUNT', '5'))
except ValueError:
    DEFAULT_SPIN_COUNT = 5
try:
    DEFAULT_SPIN_TIME = float(os.getenv('I3HUB_TEST_SPIN_TIME', '0.0001'))
except ValueError:
    DEFAULT_SPIN_TIME = 0.0001


async def spin(count=DEFAULT_SPIN_COUNT, time=DEFAULT_SPIN_TIME):
    # hacky way to wait for the event loop to spin at least `count` times. This
    # is generally more reliable than calling `await asyncio.sleep()` in order
    # to wait for certain events that can happen after a couple of event loop
    # runs.
    remaining = count
    f = asyncio.Future()
    loop = asyncio.get_event_loop()
    def spin():
        nonlocal remaining
        remaining -= 1
        if remaining == 0:
            f.set_result(None)
        else:
            loop.call_later(time, spin)
    loop.call_later(time, spin)
    await f


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


def i3msg(msg_type, msg_payload):
    body = msg_payload.encode('utf-8')
    header = b'i3-ipc' + struct.pack('=II', len(body), msg_type)
    return header + body


def i3event(msg_type, msg_payload):
    body = msg_payload.encode('utf-8')
    header = b'i3-ipc' + struct.pack('=II', len(body), msg_type | 0x80000000)
    return header + body

