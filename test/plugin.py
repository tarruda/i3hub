from ..i3hub import listen 



class Plugin(object):
    def __init__(self, events):
        self._events = events

    def _record_event(self, i3, event, arg):
        self._events.append((i3, event, arg))


class I3Events(Plugin):
    @listen('i3hub::init')
    @listen('i3::window')
    @listen('i3::shutdown')
    async def event_handler(self, i3, event, arg):
        self._record_event(i3, event, arg)


class StatusEvents(Plugin):
    @listen('i3hub::status_update')
    @listen('i3hub::status_click')
    @listen('i3hub::status_stop')
    @listen('i3hub::status_cont')
    async def event_handler(self, i3, event, arg):
        self._record_event(i3, event, arg)

    @listen('i3hub::init')
    async def init_handler(self, i3, event, arg):
        i3.get_status().append(1)
        i3.update_status()


class PluginEvents(Plugin):
    @listen('plugin::some-plugin::custom-event')
    async def event_handler(self, i3, event, arg):
        self._record_event(i3, event, arg)
