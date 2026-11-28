"""A remote parser must not advertise options it silently drops."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from paddleocr_shim.app import create_app
from paddleocr_shim.remote import PaddleServingEngine


@pytest.mark.parametrize("options", [{"pipeline": "vl"}, {"lang": "de"}, {"heading_levels": True}])
def test_remote_schema_and_validation_do_not_offer_local_only_options(options):
    sent = []

    def upstream(request):
        sent.append(request)
        return httpx.Response(200, json={"result": {"layoutParsingResults": []}})

    engine = PaddleServingEngine("http://paddlex", client=httpx.Client(transport=httpx.MockTransport(upstream)))
    with TestClient(create_app(engine=engine)) as client:
        schema = client.get("/info").json()["options_schema"]["properties"]
        assert not {"pipeline", "lang", "heading_levels"}.intersection(schema)
        response = client.post(
            "/parse", files={"file": ("scan.png", b"pixels", "image/png")}, data={"options": json.dumps(options)}
        )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_options"
    assert not sent
