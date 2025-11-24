import unittest
import json
import os
import tempfile
import shutil
from unittest.mock import MagicMock, patch
from beetsplug.ai.llm import save_training_data
from beets import config

class TestTrainingDataGeneration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.dataset_path = os.path.join(self.test_dir, "training_data.jsonl")
        
        # Mock config
        config['llm'] = {'training_data_path': self.dataset_path}

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_save_training_data_perfect_match(self):
        query = {"title": "Numb", "artist": "Linkin Park", "album": "Meteora"}
        
        track = MagicMock()
        track.title = "Numb"
        track.originalTitle = "Linkin Park"
        track.parentTitle = "Meteora"
        
        save_training_data(query, track, self.dataset_path)
        
        with open(self.dataset_path, 'r') as f:
            line = f.readline()
            data = json.loads(line)
            
            self.assertEqual(data['instruction'], "Extract the song title, artist, and album from the text. Return JSON.")
            self.assertIn("Numb", data['input'])
            self.assertIn("Linkin Park", data['input'])
            
            output = json.loads(data['output'])
            self.assertEqual(output['title'], "Numb")
            self.assertEqual(output['artist'], "Linkin Park")
            self.assertEqual(output['album'], "Meteora")

    def test_save_training_data_partial_match(self):
        # Input query doesn't have the album
        query = {"title": "Numb", "artist": "Linkin Park"} 
        
        track = MagicMock()
        track.title = "Numb"
        track.originalTitle = "Linkin Park"
        track.parentTitle = "Meteora" # Track has album, but input doesn't
        
        save_training_data(query, track, self.dataset_path)
        
        with open(self.dataset_path, 'r') as f:
            line = f.readline()
            data = json.loads(line)
            output = json.loads(data['output'])
            
            self.assertEqual(output['title'], "Numb")
            self.assertEqual(output['artist'], "Linkin Park")
            self.assertIsNone(output['album'], "Album should be None because it wasn't in the input query")

    def test_save_training_data_dirty_input(self):
        # Input is a "dirty" search query
        query = {"title": "linkin park numb official video"}
        
        track = MagicMock()
        track.title = "Numb"
        track.originalTitle = "Linkin Park"
        track.parentTitle = "Meteora"
        
        save_training_data(query, track, self.dataset_path)
        
        with open(self.dataset_path, 'r') as f:
            line = f.readline()
            data = json.loads(line)
            output = json.loads(data['output'])
            
            self.assertEqual(output['title'], "Numb")
            self.assertEqual(output['artist'], "Linkin Park")
            self.assertIsNone(output['album'])

    def test_save_training_data_no_match_skips_save(self):
        query = {"title": "Something Else"}
        
        track = MagicMock()
        track.title = "Numb"
        track.originalTitle = "Linkin Park"
        track.parentTitle = "" # Ensure this is a string, not a Mock
        
        save_training_data(query, track, self.dataset_path)
        
        # File should not exist or be empty because nothing matched
        if os.path.exists(self.dataset_path):
            with open(self.dataset_path, 'r') as f:
                content = f.read()
                self.assertEqual(content, "", "Should not save if no fields match input")

if __name__ == '__main__':
    unittest.main()
