"""Tier 5 Adversarial Browser Regression Tests (Playwright Chromium)."""

from __future__ import annotations

from tests_e2e.conftest import (
    DEFAULT_STATS_TOKEN,
    IsolatedUvicornServer,
    PlaywrightWorker,
)


def _assert_clean_runtime(res: dict) -> None:
    assert res.get("ok") is True, res.get("error")
    errors = res["errors"]
    assert errors["consoleErrors"] == [], f"Console errors: {errors['consoleErrors']}"
    assert errors["pageErrors"] == [], f"Page errors: {errors['pageErrors']}"
    assert errors["failedRequests"] == [], f"Failed requests: {errors['failedRequests']}"
    assert errors["httpErrors"] == [], f"HTTP >=400 errors: {errors['httpErrors']}"
    assert errors["thirdPartyRequests"] == [], f"Third-party requests: {errors['thirdPartyRequests']}"


def test_browser_t5_plays_debounce_clear_and_stale_abort(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    res = pw_worker.call(
        "run_tier5_probe",
        probe="plays_debounce_clear_and_stale_abort",
        baseUrl=std_server.base_url,
        viewport={"width": 1440, "height": 900},
    )
    _assert_clean_runtime(res)
    assert res["seenInitialDelayed"] is True
    assert res["initialPlaysReqCount"] >= 2
    initial_reentry = res["reentryInitialState"]
    assert initial_reentry["currentView"] == "plays"
    assert initial_reentry["statusHidden"] is True
    assert "Loading play history" not in (initial_reentry["statusText"] or "")
    assert initial_reentry["topCount"] >= 4

    clear = res["clearState"]
    assert clear["inputValue"] == ""
    assert clear["clearHidden"] is True
    assert clear["filterNoteHidden"] is True
    assert clear["topCount"] >= 4

    assert res["seenDelayedQuery"] is True
    after = res["afterAbortState"]
    assert after["currentView"] == "status"
    assert "Billie" not in (after["filterNoteText"] or "")
    assert after["topCount"] >= 4
    assert after["spatialHasPlays"] is True

    assert res["billieReqCount"] >= 2
    assert res["reentrySearchStatus"] == 200
    search_reentry = res["reentrySearchState"]
    assert search_reentry["currentView"] == "plays"
    assert search_reentry["inputValue"] == "Billie"
    assert search_reentry["clearHidden"] is False
    assert search_reentry["filterNoteHidden"] is False
    assert "Billie" in (search_reentry["filterNoteText"] or "")
    assert search_reentry["topCount"] == 1
    assert search_reentry["firstTitle"] == "Bad Guy"


def test_browser_t5_admin_vault_mobile_populated_and_auth(
    pw_worker: PlaywrightWorker,
) -> None:
    with IsolatedUvicornServer(
        {"STATS_TOKEN": DEFAULT_STATS_TOKEN},
        seed_plays=False,
        seed_artwork=False,
        seed_stats=False,
    ) as srv:
        res = pw_worker.call(
            "run_tier5_probe",
            probe="admin_vault_mobile_populated_and_auth",
            baseUrl=srv.base_url,
            statsToken=DEFAULT_STATS_TOKEN,
            viewport={"width": 390, "height": 844},
        )
        unexpected_console = [
            msg
            for msg in res["errors"]["consoleErrors"]
            if "401 (Unauthorized)" not in msg
        ]
        assert unexpected_console == [], f"Unexpected console errors: {unexpected_console}"
        res["errors"]["consoleErrors"] = []
        _assert_clean_runtime(res)
        assert res["authErrState"]["ariaInvalid"] == "true"
        assert "not accepted" in res["authErrState"]["errText"]

        metrics = res["layoutMetrics"]
        assert metrics["qobuzCards"] == 1
        assert metrics["docScrollWidth"] <= metrics["innerWidth"]
        assert metrics["tidalPressed"] == "true"
        for btn in metrics["btns"]:
            assert btn["width"] >= 44 and btn["height"] >= 44, btn

        lock = res["lockState"]
        assert lock["loginVisible"] is True
        assert lock["consoleHidden"] is True
        assert lock["activeId"] == "vault-key"


def test_browser_t5_forced_colors_accessibility(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    res = pw_worker.call(
        "run_tier5_probe",
        probe="forced_colors_accessibility",
        baseUrl=std_server.base_url,
        viewport={"width": 1440, "height": 900},
    )
    _assert_clean_runtime(res)
    sm = res["statusMetrics"]
    assert sm["logoAdjust"] == "none"
    assert sm["logoLoaded"] is True
    assert sm["pillOnAdjust"] == "none"
    assert sm["pillOnBg"] != sm["pillOffBg"]
    assert sm["tBarAdjust"] == "none"
    assert sm["tBarBorder"] != "0px"
    assert sm["dBarAdjust"] == "none"
    assert sm["dBarBorder"] != "0px"

    pm = res["playsMetrics"]
    assert pm["activeId"] == "plays-search"
    assert pm["outlineStyle"] != "none"
    assert pm["outlineWidth"] != "0px"


def test_browser_t5_webgl_context_loss_and_fallbacks(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    res = pw_worker.call(
        "run_tier5_probe",
        probe="webgl_context_loss_and_fallbacks",
        baseUrl=std_server.base_url,
        viewport={"width": 1440, "height": 900},
    )
    _assert_clean_runtime(res)
    assert res["errorsNoCanvas"]["consoleErrors"] == []
    assert res["errorsNoCanvas"]["pageErrors"] == []

    st = res["css3dStatus"]
    assert st["backend"] == "css-3d"
    assert st["framesRendered"] >= 1
    assert st["canvasHidden"] is True
    assert st["stageHidden"] is True
    assert st["rackTransformStyle"] == "preserve-3d"
    assert st["rackPlanes"] == 3
    assert st["restingCamera"]["rx"] == 0.0
    assert st["restingCamera"]["ry"] == 0.0

    sel = res["selectedState"]
    assert sel["selectedNode"] == "qobuz"
    assert sel["activeNodeAttr"] == "qobuz"
    assert sel["qobuzActive"] is True

    mt = res["motionToggle"]
    assert mt["redB"]["running"] is False
    assert mt["redB"]["reducedMotion"] is True
    assert mt["redB"]["frames"] == mt["redA"]
    assert mt["resC"]["reducedMotion"] is False
    assert mt["resC"]["frames"] > mt["redB"]["frames"]

    fm = res["flowModes"]
    assert fm["out"]["mode"] == "outage"
    assert "offline" in fm["out"]["readout"].lower()
    assert fm["dual"]["mode"] == "dual"
    assert "Stream round" in fm["dual"]["readout"]
    assert fm["failover"]["mode"] == "failover"
    assert fm["failover"]["target"] == "qobuz"
    assert "Failover" in fm["failover"]["readout"]
    assert fm["degraded"]["mode"] == "degraded"
    assert "Degraded" in fm["degraded"]["readout"]
    assert fm["single"]["mode"] == "single"
    assert "Active route" in fm["single"]["readout"]

    assert res["plays3d"]["hasUnit"] is True
    assert res["plays3d"]["hasDisc"] is True
    assert res["plays3d"]["transformStyle"] == "preserve-3d"

    assert res["about3d"]["hasStack"] is True
    assert res["about3d"]["plates"] == 3
    assert res["about3d"]["transformStyle"] == "preserve-3d"

    assert res["noCanvasBackend"] == "css-3d"


def test_browser_t5_mobile_touch_targets_anchors_and_polling(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    res = pw_worker.call(
        "run_tier5_probe",
        probe="mobile_touch_anchors_30d_and_polling",
        baseUrl=std_server.base_url,
        viewport={"width": 390, "height": 844},
    )
    _assert_clean_runtime(res)
    qbox = res["quietBox"]
    assert qbox is not None
    assert qbox["width"] >= 44 and qbox["height"] >= 44, qbox

    c30 = res["chart30d"]
    assert c30["afterArrow"]["activeIsSecond"] is True
    assert c30["afterArrow"]["readout"] != c30["readoutOnFirst"]
    assert c30["readoutAfterFocusout"] != c30["afterArrow"]["readout"]

    assert res["polling"]["statsRequestsOnAbout"] == 0
    assert res["polling"]["statsRequestsOnReturn"] >= 1

    don = res["donationsState"]
    assert don["activeId"] == "donations-bay"
    assert don["scrollY"] > 100
    assert don["bayTop"] is not None and abs(don["bayTop"]) < 140
