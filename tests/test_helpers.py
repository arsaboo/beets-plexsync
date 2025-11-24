import importlib
import sys
import types
import unittest


class DummyConfigNode:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        if isinstance(self._data, dict) and key in self._data:
            return DummyConfigNode(self._data[key])
        raise NotFoundError(key)

    def get(self, cast=None):
        value = self._data
        if isinstance(value, DummyConfigNode):
            value = value._data
        if cast is None or value is None:
            return value
        if cast is bool:
            return bool(value)
        return cast(value)


class DummyConfig(DummyConfigNode):
    def __init__(self):
        super().__init__({})

    def set_data(self, data):
        self._data = data


class NotFoundError(Exception):
    pass


class ConfigValueError(Exception):
    pass


def ensure_stubs(data):
    config = DummyConfig()
    config.set_data(data)

    beets = types.ModuleType('beets')
    ui_module = types.ModuleType('beets.ui')

    def colorize(_name, text):
        return text

    ui_module.colorize = colorize
    beets.ui = ui_module
    beets.config = config

    confuse = types.ModuleType('confuse')
    confuse.NotFoundError = NotFoundError
    confuse.ConfigValueError = ConfigValueError

    sys.modules['beets'] = beets
    sys.modules['beets.ui'] = ui_module
    sys.modules['confuse'] = confuse

    return config


class GetPlexsyncConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = ensure_stubs({'plexsync': {}})
        if 'beetsplug.helpers' in sys.modules:
            importlib.reload(sys.modules['beetsplug.helpers'])
        else:
            importlib.import_module('beetsplug.helpers')

    def test_default_value_returned(self):
        helpers = importlib.import_module('beetsplug.helpers')
        self.assertTrue(helpers.get_plexsync_config('manual_search', bool, True))

    def test_nested_lookup(self):
        self.config.set_data({'plexsync': {'playlists': {'items': [1, 2]}}})
        helpers = importlib.import_module('beetsplug.helpers')
        self.assertEqual(
            helpers.get_plexsync_config(['playlists', 'items'], list, []),
            [1, 2],
        )

    def test_missing_nested_returns_default(self):
        helpers = importlib.import_module('beetsplug.helpers')
        self.assertEqual(
            helpers.get_plexsync_config(['playlists', 'defaults'], dict, {'x': 1}),
            {'x': 1},
        )


if __name__ == '__main__':
    unittest.main()
