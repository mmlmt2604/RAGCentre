"""The IR page list must describe the parsed window, including selected blank pages."""

import sys
import types

import pytest
from fastapi.testclient import TestClient

from mineru_shim.app import create_app
from mineru_shim.engine import MineruEngine


@pytest.mark.parametrize("remote", [False, True])
def test_page_window_omits_unparsed_pages_and_keeps_selected_blank_page(monkeypatch, remote):
    result = types.SimpleNamespace(
        middle_json=types.SimpleNamespace(pages=[types.SimpleNamespace(page_idx=1), types.SimpleNamespace(page_idx=2)])
    )
    module = types.ModuleType("mineru")
    module.parse = lambda *args, **kwargs: result

    class ApiParser:
        def __init__(self, **kwargs):
            pass

        def parse(self, *args, **kwargs):
            return result

    module.MinerUApiParser = ApiParser
    render = types.ModuleType("mineru.render")
    content = types.ModuleType("mineru.render.content_list")
    content.render_content_list = lambda middle: [{"type": "text", "text": "page two", "page_idx": 1}]
    for entry in (module, render, content):
        monkeypatch.setitem(sys.modules, entry.__name__, entry)
    monkeypatch.setattr("mineru_shim.engine.page_sizes", lambda *args: {n: (600, 800) for n in range(1, 6)})
    app = create_app(MineruEngine(api_url="http://mineru.invalid" if remote else None))
    response = TestClient(app).post(
        "/parse",
        files={"file": ("report.pdf", b"pdf", "application/pdf")},
        data={"options": '{"tier":"flash","page_range":"2-3"}'},
    )
    assert response.status_code == 200, response.text
    ir = response.json()
    assert [page["page"] for page in ir["pages"]] == [2, 3]
    assert [block["page"] for block in ir["blocks"]] == [2]
