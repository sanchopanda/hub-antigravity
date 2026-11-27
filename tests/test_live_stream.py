"""Промежуточный текст виден во время хода и не подменяет итог ленты."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import bridge
import live_stream
import translate


class LiveStream(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'HUB_MOD_ANTIGRAVITY_DATA': self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_isolation_expiry_and_clear(self):
        live_stream.write('одна', 'первый фрагмент')
        self.assertEqual(live_stream.read('одна'), 'первый фрагмент')
        self.assertEqual(live_stream.read('другая'), '')
        self.assertEqual(live_stream.read('одна', now=time.time() + 121), '')
        live_stream.write('одна', '')
        self.assertEqual(live_stream.read('одна'), '')

    def test_chunks_visible_before_completed_and_not_in_final_twice(self):
        b = bridge.Bridge.__new__(bridge.Bridge)
        b.heard, b.mono = 0, time.monotonic
        b.session, b.uuid, b.model_name, b.model_id = 'тест', 'uuid', 'model', 'model'
        b.turn = translate.Turn('uuid', 'model')
        emitted = []
        b.emit = emitted.append
        for text in ('Первый ', 'фрагмент'):
            b.on_agy({'event':'step_update', 'step_type':'agent_response', 'step_index':1,
                      'state':'ACTIVE', 'text_delta':text})
        self.assertEqual(live_stream.read('тест'), 'Первый фрагмент')
        self.assertEqual(emitted, [])
        b.on_agy({'event':'step_update', 'step_type':'agent_response', 'step_index':1,
                  'state':'DONE', 'text_delta':' готов.'})
        self.assertEqual(live_stream.read('тест'), '')
        self.assertEqual([m['message']['content'][0]['text'] for m in emitted], ['Первый фрагмент готов.'])


if __name__ == '__main__':
    unittest.main()
