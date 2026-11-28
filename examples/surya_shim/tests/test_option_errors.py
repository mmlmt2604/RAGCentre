"""Invalid options must fail before an unavailable inference server is started."""

from fastapi.testclient import TestClient
from shimkit import ShimError

from surya_shim.app import create_app


def test_page_range_on_an_image_is_rejected_even_when_the_engine_is_down():
    called = []

    def down(*args):
        called.append(True)
        raise ShimError(503, "engine_unavailable", "inference server is offline")

    with TestClient(create_app(engine=down)) as client:
        response = client.post(
            "/parse", files={"file": ("scan.png", b"pixels", "image/png")}, data={"options": '{"page_range":[1,1]}'}
        )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_options"
    assert not called
