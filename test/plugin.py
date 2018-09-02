from ..i3hub import listen 


class Plugin(object):
    def __init__(self, events):
        self._events = events

    @listen('i3hub::init')
    @listen('i3::window')
    @listen('i3::shutdown')
    @listen('plugin::some-plugin::custom-event')
    async def event_handler(self, i3, event, arg):
        self._events.append((i3, event, arg))
