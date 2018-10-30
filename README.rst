i3hub
=====

.. image:: https://circleci.com/gh/tarruda/i3hub.svg?style=svg
    :target: https://circleci.com/gh/tarruda/i3hub

i3hub is a framework for extending the i3 window manager by writing Python 3.5+
coroutines. Features:

- A single connection to i3 is shared for all extensions and managed by an
  asyncio event loop.
- i3 extensions are scripts that specify coroutine functions (defined with
  python 3.5+ async/await syntax) as event handlers.
- Custom events can also be emitted and handled by other extensions, exposing a
  modular way to reuse and modularize extension code.
- i3hub also implements the i3bar protocol and can be run as a status command.
  This provides a clean/structured way to extend i3status or any other status
  command, and even implement custom status behavior from the scratch.
- Flexible configuration file format supporting includes and per-extension
  configuration sections.

Installation
------------

.. code-block::

    pip3 install i3hub


Usage
-----

Simply run `i3hub` when i3 starts by adding `exec i3hub` in your i3 config.
Another (better) option is to run it as an i3bar status command, which
allows an integrated i3bar + i3 extension experience:

.. code-block::

    bar {
            tray_output "primary"
            status_command "exec i3hub --run-as-status"
    }

Configuration
-------------

i3hub supports system-wide and per-user configuration files. The main
configuration file is searched in the following locations:

- ~/.config/i3hub/i3hub.cfg
- /etc/xdg/i3hub/i3hub.cfg
- /usr/local/share/i3hub/i3hub.cfg
- /usr/share/i3hub/i3hub.cfg

The first one found is used as the main configuration file, the remaining are
ignored.


Example
-------

Adapted from i3ipc `app-on-ws-init.py example
<https://github.com/acrisci/i3ipc-python/blob/master/examples/app-on-ws-init.py>`_,
this extension opens an application when a specific workspace is entered:

.. code-block:: python

    from i3hub import listen 
    
    @listen('i3::workspace')
    async def on_workspace(i3, event, arg):
        if arg['current']['num'] == 6 and arg['change'] == 'init':
            await i3.command('exec i3-sensible-terminal')

In the above example, the command and workspace are hardcoded in the extension.
Here's a more flexible version that uses i3hub.cfg to allow an arbitrary list of
i3 commands to be executed on demand when a workspace is created:


.. code-block:: python

    import asyncio
    
    from i3hub import listen 
    
    workspace_cmd_map = {}
    
    @listen('i3hub::init')
    async def on_init(i3, event, arg):
        # arg['config'] will contain configuration options set in the extension
        # section in the config file
        workspace_cmd_map.update(arg['config'].get('workspaces', {}))
    
    @listen('i3::workspace')
    async def on_workspace(i3, event, arg):
        if arg['change'] != 'init':
            # only run when workspace is initializing
            return
        # fetch all commands configured to run on this workspace
        cmds_for_workspace = workspace_cmd_map.get(arg['current']['name'], [])
        for cmd in cmds_for_workspace:
            print('executing', cmd)
            reply = await i3.command(cmd)
            if not reply[0]['success']:
                print('failed to execute', cmd, file=sys.stderr)
            if cmd[0:4] == 'exec':
                # wait some time for the command to create its windows
                await asyncio.sleep(0.2)


Assuming the above script is saved as
~/.config/i3hub/extensions/workspace_setup.py, here's an example i3hub.cfg:


.. code-block::

    [i3hub]
    # extensions have to be explicitly listed
    extensions = [
      "workspace_setup"
      ]
    
    [workspace_setup]
    workspaces = {
        "6": ["exec i3-sensible-terminal"],
        "7": [
          "exec urxvt -e vim",
          "split vertical",
          "exec urxvt -e htop",
          "split horizontal",
          "exec urxvt"
        ]
      }

i3hub configuration file uses python `configparse
<https://docs.python.org/3/library/configparser.html>`_ format, but the values
can have json notation which are parsed automatically. Extensions are named
after the python module which implements it, which is also the name of the
configuration section that will be passed to the "i3hub::init" event handler.

When i3hub is running with the --run-as-status flag, all output, including from
extensions, will be logged to $XDG_RUNTIME_DIR/i3hub.log (usually
/run/user/UID/i3hub.log). That is required since stdout will be used to
communicate with i3bar.
