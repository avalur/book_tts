from types import SimpleNamespace

import numpy as np
import pytest

from book_tts.engines import KokoroSession


def test_kokoro_preserves_fractional_speed_for_float_model_export():
    class Export:
        def get_inputs(self):
            return [SimpleNamespace(name="speed", type="tensor(float)")]

        def run(self, names, inputs):
            assert inputs["speed"].dtype == np.float32
            assert inputs["speed"][0] == pytest.approx(1.15)
            return [np.ones(24000)]

    session = KokoroSession(Export())
    session.speed = 1.15
    assert len(session.run(None, {"speed": np.array([1], dtype=np.int32)})[0]) == 24000
