import json
import logging
from dataclasses import replace

import httpx
import pytest
import respx

from app import bugs
from tests.conftest import make_services

CONTEXT = {
    "actions": [
        {"t": "12:00:01", "screen": "receipt"},
        {"t": "12:00:05", "api": "POST /products/new/label-photo", "status": 400},
    ],
    "errors": ["Grocy: QU conversion already exists"],
    "device": {"userAgent": "iPhone"},
}


@pytest.mark.asyncio
async def test_report_is_stored_with_server_log(settings, catalog, fake_grocy, tmp_path):
    bugs.install_log_buffer()
    logging.getLogger("app.products").warning("something odd")
    svc = make_services(replace(settings, db_path=str(tmp_path / "c.db")), catalog, fake_grocy)
    r = await bugs.report(svc, "Label photo fails for Coca-Cola", "label", CONTEXT, b"jpgdata")
    assert r == {"id": 1, "github_url": None}
    saved = svc.db.list_bugs()[0]
    assert saved["description"] == "Label photo fails for Coca-Cola" and saved["screen"] == "label"
    assert saved["context"]["errors"] == CONTEXT["errors"]
    assert any("something odd" in line for line in saved["context"]["server_log"])
    assert (tmp_path / "bugs" / "bug-1.jpg").read_bytes() == b"jpgdata"


@pytest.mark.asyncio
@respx.mock
async def test_report_becomes_a_github_issue(settings, catalog, fake_grocy, tmp_path):
    s = replace(
        settings, db_path=str(tmp_path / "c.db"), bug_github_repo="me/bugs", bug_github_token="t", version="abc1234"
    )
    shot = respx.put("https://api.github.com/repos/me/bugs/contents/screenshots/bug-1.jpg").respond(201, json={})
    issue = respx.post("https://api.github.com/repos/me/bugs/issues").respond(
        201, json={"html_url": "https://github.com/me/bugs/issues/7"}
    )
    svc = make_services(s, catalog, fake_grocy)
    r = await bugs.report(svc, "Back button stuck\nmore detail", "receipt", CONTEXT, b"img")
    assert r["github_url"] == "https://github.com/me/bugs/issues/7"
    sent = json.loads(issue.calls[0].request.content)
    assert sent["title"] == "Bug: Back button stuck" and sent["labels"] == ["bug-report"]
    assert "abc1234" in sent["body"] and sent["body"].startswith("![screenshot]")
    assert shot.called
    assert svc.db.list_bugs()[0]["github_url"] == "https://github.com/me/bugs/issues/7"


@pytest.mark.asyncio
@respx.mock
async def test_github_down_still_keeps_the_report(settings, catalog, fake_grocy, tmp_path):
    s = replace(settings, db_path=str(tmp_path / "c.db"), bug_github_repo="me/bugs", bug_github_token="t")
    respx.post("https://api.github.com/repos/me/bugs/issues").mock(side_effect=httpx.ConnectError("down"))
    svc = make_services(s, catalog, fake_grocy)
    r = await bugs.report(svc, "Something broke", None, {}, None)
    assert r == {"id": 1, "github_url": None}
