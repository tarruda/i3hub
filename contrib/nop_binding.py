import shlex

from i3hub import listen


@listen('i3::binding')
async def on_binding(i3, event, arg):
    if arg['change'] != 'run':
        return
    argv = shlex.split(arg['binding']['command'])
    if argv[0] != 'nop' or len(argv) < 2:
        return
    await i3.emit_event('nop-binding::{}'.format(argv[1]), argv[2:])
