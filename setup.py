from setuptools import setup

setup(
    name='beets-plexsync',
    version='0.1',
    description='beets plugin to sync with Plex',
    long_description=open('README.md').read(),
    author='Alok Saboo',
    author_email='',
    url='https://github.com/arsaboo/beets-plexsync',
    license='MIT',
    platforms='ALL',
    packages=['beetsplug'],
    install_requires=[
        'beets>=1.6.0',
        'plexapi>=4.13.4',
        'jiosaavn-python>=0.2',
        'spotipy',
        'openai',
        'pydantic>=2.0.0',
        'python-dateutil',
        'confuse',
        'requests',
        'beautifulsoup4',
        'pillow',
        'json_repair',
    ],
)
