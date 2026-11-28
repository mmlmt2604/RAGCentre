"""Changing strategies must release old model pipelines before loading new ones."""

import sys
import types
import weakref

from paddleocr_shim.engine import PaddleEngine


def test_strategy_switch_releases_previous_pipeline_before_loading(monkeypatch):
    references = []
    constructions = []

    class Pipeline:
        def __init__(self, **kwargs):
            assert all(ref() is None for ref in references), "old models are still retained while loading new ones"
            references.append(weakref.ref(self))
            constructions.append(kwargs)
            self.cycle = self  # model wrappers can contain reference cycles

        def predict(self, path, **kwargs):
            return []

        def restructure_pages(self, results, **kwargs):
            return results

    module = types.ModuleType("paddleocr")
    module.PPStructureV3 = Pipeline
    module.PaddleOCRVL = Pipeline
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    engine = PaddleEngine()
    assert engine(b"x", "a.pdf", {"tables": True}) == []
    assert engine(b"x", "a.pdf", {"tables": True}) == []
    assert len(constructions) == 1
    assert engine(b"x", "a.pdf", {"tables": False}) == []
    assert engine(b"x", "a.pdf", {"pipeline": "vl"}) == []
    assert engine(b"x", "a.pdf", {"tables": True}) == []
    assert len(constructions) == 4
