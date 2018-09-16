from ..i3hub import extension, listen 


class Extension(object):
    def __init__(self, i3):
        self._i3 = i3
        self._events = []

    def _record_event(self, event, arg):
        self._events.append((self._i3, event, arg))


@extension
class I3Events(Extension):
    @listen('i3hub::init')
    @listen('i3::window')
    @listen('i3::shutdown')
    async def event_handler(self, event, arg):
        self._record_event(event, arg)


@extension
class StatusEvents(Extension):
    @listen('i3hub::status_update')
    @listen('i3hub::status_click')
    @listen('i3hub::status_stop')
    @listen('i3hub::status_cont')
    async def event_handler(self, event, arg):
        self._record_event(event, arg)

    @listen('i3hub::init')
    async def init_handler(self, event, arg):
        self._i3.get_status().append(1)
        self._i3.update_status()


@extension
class ExtensionEvents(Extension):
    @listen('extension::some_extension::custom')
    async def event_handler(self, event, arg):
        self._record_event(event, arg)
        arg.append('extension-data')


class ModuleExtension(object):
    def __init__(self):
        self._events = []

    @listen('i3hub::init')
    @listen('i3::window')
    @listen('i3::shutdown')
    async def event_handler(self, i3, event, arg):
        self._events.append((i3, event, arg))
