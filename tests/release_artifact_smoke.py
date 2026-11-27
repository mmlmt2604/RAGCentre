"""Smoke an exact wheel in a clean environment outside the source checkout.

Usage: python tests/release_artifact_smoke.py /absolute/path/to/ragcentre.whl
Only synthetic fixture data is created. Temporary environments and processes are cleaned up.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


def main() -> None:
    wheel = Path(sys.argv[1]).resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "ragcentre/ui_dist/index.html" in names
        assert "ragcentre/db/migrations/0005_accounts.sql" in names
        assert not any(name.endswith((".db", ".env", "e2e-credentials.json")) for name in names)
    with tempfile.TemporaryDirectory(prefix="ragcentre-release-artifact-") as directory:
        root = Path(directory)
        venv = root / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True, cwd=root, capture_output=True)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(venv / "bin/python"), str(wheel)],
            check=True,
            cwd=root,
            capture_output=True,
        )
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("RAGCENTRE_") and key != "PYTHONPATH"
        }
        environment.update(
            {"RAGCENTRE_DATA_DIR": str(root / "data"), "RAGCENTRE_HOST": "127.0.0.1", "RAGCENTRE_DISCOVERY_URLS": "off"}
        )
        subprocess.run(
            [str(venv / "bin/ragcentre"), "--help"], check=True, cwd=root, env=environment, capture_output=True
        )
        imported = subprocess.run(
            [str(venv / "bin/python"), "-I", "-c", "import ragcentre; print(ragcentre.__file__)"],
            check=True,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert str(venv) in imported.stdout, "Import must resolve to installed wheel"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        cookies = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookies), urllib.request.ProxyHandler({})
        )

        def request(
            method: str,
            path: str,
            body: Any = None,
            csrf: str | None = None,
            raw: bytes | None = None,
            content_type: str | None = None,
        ) -> tuple[int, bytes]:
            headers = {}
            data = raw
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            if content_type:
                headers["Content-Type"] = content_type
            if csrf:
                headers["X-RAGCentre-CSRF"] = csrf
            req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
            try:
                with opener.open(req, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        with (root / "server.log").open("wb") as logs:
            process = subprocess.Popen(
                [str(venv / "bin/ragcentre"), "serve", "--port", str(port), "--workers", "1"],
                cwd=root,
                env=environment,
                stdout=logs,
                stderr=logs,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        if request("GET", "/v1/health")[0] == 200:
                            break
                    except (urllib.error.URLError, OSError):
                        pass
                    time.sleep(0.05)
                assert process.poll() is None, "Installed server exited during startup"
                assert request("GET", "/")[0] == 200
                password = secrets.token_urlsafe(24)
                status, data = request("POST", "/v1/auth/setup", {"username": "artifact-user", "password": password})
                assert status == 201
                csrf = json.loads(data)["csrf"]
                assert request("GET", "/v1/auth/me")[0] == 200
                status, data = request("POST", "/v1/corpora", {"name": "artifact-corpus"}, csrf)
                assert status == 201
                cid = json.loads(data)["id"]
                boundary = secrets.token_hex(12)
                upload = (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="artifact.txt"\r\nContent-Type: text/plain\r\n\r\nA warranty covers the artifact bicycle.\r\n--{boundary}--\r\n'
                ).encode()
                assert (
                    request(
                        "POST",
                        f"/v1/corpora/{cid}/documents",
                        csrf=csrf,
                        raw=upload,
                        content_type=f"multipart/form-data; boundary={boundary}",
                    )[0]
                    == 201
                )
                config = {
                    "name": "artifact-keyword",
                    "nodes": [
                        {"id": "parse", "type": "parse", "routes": [{"mime": "text/*", "provider": "builtin-text"}]},
                        {
                            "id": "chunk",
                            "type": "chunk",
                            "inputs": ["parse"],
                            "strategy": "fixed",
                            "options": {"max_chars": 500, "overlap_chars": 0},
                        },
                        {"id": "fts", "type": "index_fts", "inputs": ["chunk"]},
                    ],
                    "retrieval": {"sources": [{"id": "keyword", "index": "fts", "top_k": 8}], "top_k": 8},
                }
                status, data = request("POST", f"/v1/corpora/{cid}/pipelines", config, csrf)
                assert status == 201
                pid = json.loads(data)["id"]
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if json.loads(request("GET", f"/v1/pipelines/{pid}/status")[1])["status"] == "ready":
                        break
                    time.sleep(0.05)
                status, data = request(
                    "POST",
                    f"/v1/corpora/{cid}/query",
                    {"pipeline_id": pid, "query": "warranty", "max_chars": 200},
                    csrf,
                )
                assert status == 200 and json.loads(data)["results"]
                result = json.loads(data)
                hit = result["results"][0]
                assert isinstance(hit["ordinal"], int) and result["truncated"] is False
                status, data = request("GET", f"/v1/chunks/{hit['chunk_id']}?before=10&after=10")
                assert status == 200 and json.loads(data)["chunk"]["id"] == hit["chunk_id"]
                status, data = request("GET", f"/v1/documents/{hit['document_id']}/outline?pipeline_id={pid}")
                assert status == 200 and json.loads(data)["sections"]
                assert request("POST", "/v1/auth/logout", csrf=csrf)[0] == 204
                assert request("GET", "/v1/auth/me")[0] == 401
                status, data = request("POST", "/v1/auth/login", {"username": "artifact-user", "password": password})
                assert status == 200
                csrf = json.loads(data)["csrf"]
                replacement = secrets.token_urlsafe(24)
                assert (
                    request(
                        "POST", "/v1/auth/password", {"current_password": password, "new_password": replacement}, csrf
                    )[0]
                    == 204
                )
                assert request("GET", "/v1/auth/me")[0] == 401
                status, data = request("POST", "/v1/auth/login", {"username": "artifact-user", "password": replacement})
                assert status == 200
                assert request("GET", "/v1/auth/me")[0] == 200
                csrf = json.loads(data)["csrf"]
                assert request("POST", "/v1/corpora", {"name": "after-password-change"}, csrf)[0] == 201
                recovered = secrets.token_urlsafe(24)
                reset = subprocess.run(
                    [str(venv / "bin/ragcentre"), "account", "reset-password"],
                    input=f"{recovered}\n{recovered}\n",
                    text=True,
                    capture_output=True,
                    cwd=root,
                    env=environment,
                    timeout=15,
                )
                assert reset.returncode == 0, "Installed CLI recovery failed"
                assert recovered not in reset.stdout and recovered not in reset.stderr
                assert request("GET", "/v1/auth/me")[0] == 401
                status, data = request("POST", "/v1/auth/login", {"username": "artifact-user", "password": recovered})
                assert status == 200
                assert request("GET", "/v1/auth/me")[0] == 200
                csrf = json.loads(data)["csrf"]
                assert request("POST", "/v1/corpora", {"name": "after-local-recovery"}, csrf)[0] == 201
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        print(
            json.dumps(
                {
                    "artifact": wheel.name,
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "result": "passed",
                    "checks": [
                        "wheel UI/migrations",
                        "clean install/import/CLI",
                        "real server outside checkout",
                        "setup/login/logout",
                        "password change and installed CLI recovery",
                        "protected reads and writes after recovery",
                        "upload/pipeline/query",
                        "query budget/ordinal, chunk window and outline",
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
