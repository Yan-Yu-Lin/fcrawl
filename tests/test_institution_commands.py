"""`papers pdf` falling through to the library proxy, `papers fetch` failures, the
clipboard paste and `auth status` limits. Offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from fcrawl.utils import institution as I
from test_institution import PDF, R, enable, run, store_with
from test_institution import env as _env_fixture

env = _env_fixture

DOI = "10.1109/TPEL.2012.2223718"
STAMP = "https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/stampPDF/getPDF.jsp"


@pytest.fixture
def no_free_copy(monkeypatch):
    from fcrawl.commands import papers as P

    monkeypatch.setattr(P, "openalex_work", lambda pid, nc: {"title": "T", "year": 2012, "ids": {"doi": DOI.lower()},
                                                              "locations": [], "oaStatus": "closed"})
    monkeypatch.setattr(P, "s2_open_pdf", lambda pid: (None, "no open PDF"))


def ieee_routes(web):
    web.routes[f"https://doi.org/{DOI.lower()}"] = R(302, headers={"Location": "https://ieeexplore.ieee.org/document/6329441"})
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/6329441"] = R(200, "<html>abs</html>")
    web.routes[STAMP] = R(200, PDF, {"Content-Type": "application/pdf"})


# --- A: papers pdf falls through to the proxy -----------------------------------------------


def test_pdf_downloads_through_the_library_when_on(env, no_free_copy):
    web, _ = env
    enable()
    store_with()
    ieee_routes(web)
    res = run("papers", "pdf", f"doi:{DOI}", "--no-scholar")
    assert res.exit_code == 0, res.output
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("RESULT ")][0]
    assert "provider=institution:ntut" in line and "status=ok" in line and f"bytes={len(PDF)}" in line
    assert Path(line.split("path=", 1)[1].split(" provider=")[0]).read_bytes() == PDF
    assert I.Usage.load("ntut").total == 1


def test_pdf_json_has_the_institution_field(env, no_free_copy):
    web, _ = env
    enable()
    store_with()
    ieee_routes(web)
    data = json.loads(run("papers", "pdf", f"doi:{DOI}", "--no-scholar", "--json").stdout)
    assert data["found"] is False and data["institution"]["ok"] is True
    assert data["institution"]["provider"] == "institution:ntut" and data["institution"]["bytes"] == len(PDF)


def test_pdf_no_institution_flag_and_feature_off(env, no_free_copy):
    web, _ = env
    store_with()
    ieee_routes(web)
    res = run("papers", "pdf", f"doi:{DOI}", "--no-scholar", "--json")
    assert json.loads(res.stdout)["institution"] is None  # off: never touched
    enable()
    res = run("papers", "pdf", f"doi:{DOI}", "--no-scholar", "--no-institution")
    assert "RESULT" not in res.stdout and web.calls == []
    assert not I.Usage.path_for("ntut").exists()


def test_pdf_pmid_uses_the_doi_openalex_found(env, no_free_copy):
    web, _ = env
    enable()
    store_with()
    ieee_routes(web)
    res = run("papers", "pdf", "pmid:123", "--no-scholar")
    assert "provider=institution:ntut" in res.stdout and f"url=https://doi.org/{DOI.lower()}" in res.stdout


def test_pdf_expired_session_spends_nothing(env, no_free_copy):
    web, _ = env
    enable()
    store_with(expires=1)
    data = json.loads(run("papers", "pdf", f"doi:{DOI}", "--no-scholar", "--json").stdout)
    assert data["institution"]["tried"] is False and "fcrawl auth login" in data["institution"]["error"]
    assert web.calls == [] and not I.Usage.path_for("ntut").exists()


def test_pdf_proxy_failure_is_reported(env, no_free_copy):
    web, _ = env
    enable()
    store_with()
    web.routes[f"https://doi.org/{DOI.lower()}"] = R(302, headers={"Location": "https://ieeexplore.ieee.org/document/1"})
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/document/1"] = \
        R(302, headers={"Location": "https://search.lib.ntut.edu.tw/login?url=x"})
    res = run("papers", "pdf", f"doi:{DOI}", "--no-scholar")
    assert res.exit_code == 0
    assert "session has expired" in res.stdout and "RESULT provider=institution" in res.stdout
    assert "status=failed" in res.stdout


# --- S6: papers fetch failures always end in a RESULT / JSON ----------------------------------


def test_fetch_network_error_prints_result(env, monkeypatch):
    enable()
    store_with()

    def boom(*a, **k):
        raise requests.ConnectTimeout("x")

    monkeypatch.setattr(I, "http_get", boom)
    res = run("papers", "fetch", "https://ieeexplore.ieee.org/document/1")
    assert res.exit_code != 0 and "status=failed" in res.stdout and "network error (ConnectTimeout)" in res.output
    res = run("papers", "fetch", "https://ieeexplore.ieee.org/document/1", "--json")
    data = json.loads(res.stdout)
    assert data["ok"] is False and "ConnectTimeout" in data["error"]
    assert "Traceback" not in res.output


def test_fetch_unwritable_output_is_one_line(env, tmp_path):
    web, _ = env
    web.routes["https://arxiv.org/pdf/1706.03762"] = R(200, PDF, {"Content-Type": "application/pdf"})
    blocker = tmp_path / "file"
    blocker.write_text("x")
    res = run("papers", "fetch", "arxiv:1706.03762", "-o", str(blocker / "sub" / "a.pdf"))
    assert res.exit_code != 0 and "status=failed" in res.stdout and "cannot write the PDF" in res.output


# --- N4: clipboard and stdin -------------------------------------------------------------


def test_login_from_clipboard(env, monkeypatch):
    web, _ = env
    enable()
    import pyperclip

    long_value = "v" * 3000  # longer than a terminal line buffer
    monkeypatch.setattr(pyperclip, "paste", lambda: f"HYSID={long_value}; JSESSIONID=j")
    web.routes["https://ieeexplore-ieee-org.search.lib.ntut.edu.tw/Xplore/home.jsp"] = R(200, "<html>IEEE</html>")
    res = run("auth", "login", "--from-clipboard")
    assert res.exit_code == 0, res.output
    assert "saved 2 cookie(s)" in res.output and long_value not in res.output
    assert {c.name: c.value for c in I.CookieStore.load("ntut").cookies}["HYSID"] == long_value
    monkeypatch.setattr(pyperclip, "paste", lambda: "")
    assert "clipboard is empty" in run("auth", "login", "--from-clipboard").output


def test_login_no_open_lists_the_fallbacks(env):
    enable()
    res = run("auth", "login", "--no-open")
    assert res.exit_code == 0
    assert "--from-clipboard" in res.output and "untested fallback" in res.output


# --- C: auth status shows the limits in force -----------------------------------------------


def test_status_shows_configured_limits(env):
    _, tmp = env
    enable()
    rc = tmp / ".fcrawlrc"
    cfg = json.loads(rc.read_text())
    cfg["institution"]["limits"] = {"daily": 99, "spacing_seconds": 15}
    rc.write_text(json.dumps(cfg))
    res = run("auth", "status", "--no-probe")
    assert "0/50 papers" in res.output and "spacing: 15s" in res.output and "hard ceiling" in res.output
    data = json.loads(run("auth", "status", "--no-probe", "--json").stdout)
    assert data["usageToday"]["limit"] == 50 and data["usageToday"]["minSpacingSeconds"] == 15
