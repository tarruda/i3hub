from ..i3hub import extension, listen 


class Extension(object):
    def __init__(self, i3):
        self._i3 = i3
        self._events = []

    def _record_event(self, event, arg):
        self._events.append((self._i3, event, arg))


@extension(name='i3')
class I3Events(Extension):
    @listen('i3hub::init')
    @listen('i3::window')
    @listen('i3::shutdown')
    async def event_handler(self, event, arg):
        self._record_event(event, arg)


@extension(name='status')
class StatusEvents(Extension):
    _I3HUB_STATUS_EXTENSION = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._refresh_count = 0

    @listen('i3hub::i3bar_click')
    @listen('i3hub::i3bar_suspend')
    @listen('i3hub::i3bar_resume')
    async def event_handler(self, event, arg):
        self._record_event(event, arg)

    @listen('i3hub::init')
    async def init_handler(self, event, arg):
        self._i3.refresh_i3bar()

    @listen('i3hub::i3bar_refresh')
    async def on_i3bar_refresh(self, event, status_array):
        if self._refresh_count == 0:
            status_array.append(1)
        elif self._refresh_count == 1:
            status_array.append({'1': 2, '3': '4'})
        elif self._refresh_count == 2:
            status_array.append({'1': 2, '3': '4'})
            status_array.append(2)
        else:
            status_array.append({'1': 2, '3': '4'})
        self._refresh_count += 1

@extension(name='events')
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
