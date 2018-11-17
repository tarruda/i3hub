import os
from setuptools import setup


VERSION = '2.1.0'
REPO    = 'https://github.com/tarruda/i3hub'


setup(
    name='i3hub',
    version=VERSION,
    description='i3 extension runtime',
    python_requires='>=3.5',
    py_modules=['i3hub'],
    data_files=[('share/i3hub/extensions', [
        'contrib/status_wrapper.py',
        'contrib/nop_binding.py',
        'contrib/keyboard_layout_switcher.py',
        'contrib/taskmaster/__init__.py',
        'contrib/taskmaster/alarm-pomodoro.oga',
        'contrib/taskmaster/alarm-rest.oga',
        ])],
    author='Thiago de Arruda',
    author_email='tpadilha84@gmail.com',
    url=REPO,
    download_url='{0}/archive/{1}.tar.gz'.format(REPO, VERSION),
    license='MIT',
    install_requires=['pyxdg'],
    entry_points='''
    [console_scripts]
    i3hub=i3hub:main
    ''',
    )
