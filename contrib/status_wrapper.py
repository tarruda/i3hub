import asyncio
import json
import signal

from i3hub import extension, listen, STOP_SIGNAL, CONT_SIGNAL


# i3 will send stop/cont signals to the process group. use preexec_fn to ensure
# the child status process ignores i3hub's own STOP/CONT signal numbers.
def ignore_sigs():
    signal.signal(STOP_SIGNAL, signal.SIG_IGN)
    signal.signal(CONT_SIGNAL, signal.SIG_IGN)


@extension
class Status(object):
    def __init__(self, i3):
        self._i3 = i3
        self._loop = i3.event_loop
        self._command = None
        self._proc = None
        self._supports_click = None
        self._stop_sig = None
        self._cont_sig = None
        self._updating = False

    async def _dispatch_update(self, line):
        status = self._i3.get_status()
        status.clear()
        status_data = json.loads(line)
        status.extend(status_data)
        self._i3.update_status()

    async def _read_status(self):
        line = await self._proc.stdout.readline()
        if not line:
            return False
        line = line.decode('utf-8', 'replace').strip()
        if line[0] == ',':
            line = line[1:]
        await self._dispatch_update(line)
        return True

    @listen('i3hub::status_stop')
    async def dispatch_stop(self, event, arg):
        self._proc.send_signal(self._stop_sig)

    @listen('i3hub::status_cont')
    async def dispatch_cont(self, event, arg):
        self._proc.send_signal(self._cont_sig)

    @listen('i3hub::status_click')
    async def dispatch_click(self, event, arg):
        click_payload = [arg]
        await self._i3.emit_event('status_wrapper::click', click_payload)
        if self._supports_click and click_payload:
            self._proc.stdin.write(json.dumps(click_payload[0]).encode(
                'utf-8'))

    @listen('i3::shutdown')
    async def shutdown(self, event, arg):
        self._proc.terminate()
        await self._proc.wait()

    @listen('i3hub::init')
    async def init(self, event, config):
        self._command = config.get('status-command', ['i3status'])
        self._loop.create_task(self.run())

    async def run(self):
        self._proc = await asyncio.create_subprocess_exec(*self._command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                preexec_fn=ignore_sigs)
        # read first line with information about the status program
        info = json.loads((await self._proc.stdout.readline()).decode(
                'utf-8', 'replace').strip())
        # detect if the underlying status command supports click events
        self._supports_click = info.get('click_events', False)
        # detect the stop/cont signals
        self._stop_sig = info.get('stop_signal', signal.SIGSTOP)
        self._cont_sig = info.get('cont_signal', signal.SIGCONT)
        # ignore second line line with opening bracket
        await self._proc.stdout.readline()
        # dispatch initial status state
        status_read = await self._read_status()
        while status_read:
            status_read = await self._read_status()
