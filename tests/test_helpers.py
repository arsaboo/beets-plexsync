import importlib
import sys
import types
import unittest

# Import the actual function to be tested
from beetsplug.core.config import get_plexsync_config


import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock

# Import the actual function to be tested
from beetsplug.core.config import get_plexsync_config

# Import real confuse exceptions to use in mocks
import confuse

def ensure_stubs(data):
    # Mock beets.config to behave like a confuse.Configuration object
    # We create a nested structure of MagicMocks to simulate confuse.Configuration and Subview objects
    
    mock_conf = MagicMock(spec=confuse.Configuration)

    # Function to create a mock Subview for a given dictionary/value
    def create_subview(value):
        mock_subview = MagicMock(spec=confuse.Subview)
        if isinstance(value, dict):
            # If it's a dictionary, __getitem__ should return another subview
            mock_subview.__getitem__.side_effect = lambda k: create_subview(value.get(k))
            mock_subview.get.side_effect = lambda cast=None, default=None: (
                value if value is not None else default
            )
        else:
            # If it's a simple value, .get() should return it
            mock_subview.get.side_effect = lambda cast=None, default=None: (
                (cast(value) if cast else value) if value is not None else default
            )
        return mock_subview

    # The top-level beets.config[key] should return a subview
    mock_conf.__getitem__.side_effect = lambda key: create_subview(data.get(key))
    mock_conf.get.side_effect = lambda cast=None, default=None: (
        data if data is not None else default
    )

    beets = types.ModuleType('beets')
    ui_module = types.ModuleType('beets.ui')

    def colorize(_name, text):
        return text

    ui_module.colorize = colorize
    beets.ui = ui_module
    beets.config = mock_conf # Assign our mock config

    # Mock confuse module for NotFoundError
    mock_confuse = MagicMock()
    mock_confuse.NotFoundError = confuse.NotFoundError
    mock_confuse.ConfigValueError = confuse.ConfigValueError

    sys.modules['beets'] = beets
    sys.modules['beets.ui'] = ui_module
    sys.modules['confuse'] = mock_confuse

    return mock_conf # Return the top-level mock_conf for direct manipulation within tests




class GetPlexsyncConfigTest(unittest.TestCase):
    def test_default_value_returned(self):
        # Create a fresh mock config for this test
        ensure_stubs({'plexsync': {'manual_search': False}})
        self.assertFalse(get_plexsync_config('manual_search', bool, True))
        
        # Test with a config where the key is truly missing
        ensure_stubs({'plexsync': {}}) # Reset config to only 'plexsync': {}
        self.assertTrue(get_plexsync_config('some_missing_key', bool, True))

    def test_nested_lookup(self):
        ensure_stubs({'plexsync': {'playlists': {'items': [1, 2]}}})
        self.assertEqual(
            get_plexsync_config(['playlists', 'items'], list, []),
            [1, 2],
        )

    def test_missing_nested_returns_default(self):
        ensure_stubs({'plexsync': {}}) # Config without nested 'playlists'
        self.assertEqual(
            get_plexsync_config(['playlists', 'defaults'], dict, {'x': 1}),
            {'x': 1},
        )


if __name__ == '__main__':
    unittest.main()
