"""Tier 1 & Tier 2 Playwright Chromium Browser E2E Tests."""

from __future__ import annotations

import pytest

from tests_e2e.conftest import (
    DEFAULT_PATH_SECRET,
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


def _assert_clean_images_and_scroll(res: dict) -> None:
    assert res["images"]["broken"] == [], f"Broken images: {res['images']['broken']}"
    assert res["scrollAudit"]["overflowFailures"] == [], (
        f"Horizontal overflow: {res['scrollAudit']['overflowFailures']}"
    )
    assert res["scrollAudit"]["strandedFailures"] == [], (
        f"Stranded invisible elements: {res['scrollAudit']['strandedFailures']}"
    )


@pytest.mark.parametrize("route", ["/", "/status", "/monitor", "/plays", "/support", "/about"])
@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 1440, "height": 900},
        {"width": 390, "height": 844},
    ],
    ids=["desktop_1440x900", "mobile_390x844"],
)
def test_browser_t1_routes_hygiene_overflow_and_scroll(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
    route: str,
    viewport: dict[str, int],
) -> None:
    """Verify every public route renders cleanly with zero errors, broken images, overflow, or stranded elements."""
    res = pw_worker.call(
        "audit_route",
        baseUrl=std_server.base_url,
        path=route,
        viewport=viewport,
        colorScheme="dark",
        reducedMotion="no-preference",
        checkScroll=True,
        checkDrag=False,
    )
    _assert_clean_runtime(res)
    assert res["status"] == 200
    assert res["domChecks"]["bodyHasGateway"] is True
    _assert_clean_images_and_scroll(res)


def test_browser_t1_plays_contract_hooks_and_offline_artwork(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """Verify /plays exposes all 9 #plays-* hooks, renders seeded rows, and loads /art/ images offline."""
    res = pw_worker.call(
        "audit_route",
        baseUrl=std_server.base_url,
        path="/plays",
        viewport={"width": 1440, "height": 900},
        colorScheme="dark",
        reducedMotion="no-preference",
        checkScroll=True,
    )
    _assert_clean_runtime(res)
    _assert_clean_images_and_scroll(res)
    for hook, present in res["domChecks"]["playsIds"].items():
        assert present is True, f"Missing required hook #{hook}"

    ctrl = pw_worker.call(
        "check_plays_controls",
        baseUrl=std_server.base_url,
        viewport={"width": 1440, "height": 900},
    )
    _assert_clean_runtime(ctrl)
    assert ctrl["images"]["broken"] == []
    assert ctrl["counts"]["topCount"] >= 1
    assert ctrl["counts"]["recentCount"] >= 1
    assert ctrl["counts"]["chartCols"] == 24
    assert ctrl["counts"]["artImages"] >= 1
    assert ctrl["readoutAfterClick"] != ctrl["readoutBefore"]
    assert ctrl["readoutAfterArrow"] != ""
    assert ctrl["readoutAfterArrow"] != ctrl["readoutAfterClick"]
    assert ctrl["readoutAfterArrow"] != ctrl["readoutBefore"]
    assert ctrl["pressedOn"] == "true"
    assert ctrl["pressedOff"] == "false"
    if ctrl["searchTested"]:
        assert "Instant Crush" in ctrl["searchTopText"]
    assert ctrl["refreshOk"] is True


def test_browser_t1_3d_stage_active_and_pointer_drag(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """M2/R1 Contract: Verify active WebGL2/Canvas2D/CSS3D stage renders non-blank pixels & responds to pointer drag."""
    res = pw_worker.call(
        "audit_route",
        baseUrl=std_server.base_url,
        path="/",
        viewport={"width": 1440, "height": 900},
        colorScheme="dark",
        reducedMotion="no-preference",
        checkDrag=True,
        checkScroll=False,
    )
    _assert_clean_runtime(res)
    spatial = res["spatial"]
    assert spatial["active3d"] is True, "Expected active 3D canvas or CSS 3D layer"
    if spatial["hasCanvas"]:
        stats = spatial["canvasStats"]
        assert stats is not None
        assert stats["distinctColors"] >= 4, f"Canvas appears blank/monochrome: {stats}"
        assert stats["nonBgFraction"] >= 0.01, f"Canvas lacks visual variance: {stats}"
        assert (spatial["rafFrames"] or 0) >= 2, f"Expected active rAF loop, got {spatial['rafFrames']}"
    else:
        assert spatial["css3dCount"] > 0
    assert spatial["dragChanged"] is True, "3D stage did not respond to pointer drag or live frame update"
    assert spatial["dragLayoutStable"] is True, "Pointer drag caused horizontal overflow or scroll jump"


def test_browser_t2_reduced_motion_static_3d_and_zero_transitions(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """R2 Contract: Under prefers-reduced-motion: reduce, 3D renders a crisp static frame and transitions collapse."""
    res = pw_worker.call(
        "audit_route",
        baseUrl=std_server.base_url,
        path="/",
        viewport={"width": 1440, "height": 900},
        colorScheme="dark",
        reducedMotion="reduce",
        checkDrag=False,
        checkScroll=True,
    )
    _assert_clean_runtime(res)
    _assert_clean_images_and_scroll(res)
    spatial = res["spatial"]
    assert spatial["active3d"] is True, "3D stage must remain rendered (static frame) under prefers-reduced-motion: reduce"
    assert spatial["reducedTransitionsOk"] is not None
    assert spatial["reducedTransitionsOk"]["matches"] is True
    assert spatial["reducedTransitionsOk"]["slowEls"] == [], (
        f"Animations/transitions still active under reduce: {spatial['reducedTransitionsOk']['slowEls']}"
    )
    if spatial["hasCanvas"]:
        assert spatial["canvasStats"]["distinctColors"] >= 4
        assert spatial["reducedStatic"] is True, "3D canvas kept animating under prefers-reduced-motion: reduce"


def test_browser_t1_spa_pushstate_popstate_navigation(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """M2/R2 Contract: Navigating Status -> Plays -> About & Support -> Back/Forward uses SPA pushState/popstate."""
    res = pw_worker.call(
        "check_spa_nav",
        baseUrl=std_server.base_url,
        startPath="/",
        viewport={"width": 1440, "height": 900},
    )
    _assert_clean_runtime(res)
    assert res["playsStep"]["pathname"] == "/plays"
    assert res["playsStep"]["spaAlive"] is True, "Navigating to /plays triggered a full page reload"
    assert res["playsStep"]["ariaCurrentHref"] == "/plays"
    assert res["playsStep"]["heading"] != ""

    assert res["supportStep"]["pathname"] in ("/support", "/about")
    assert res["supportStep"]["spaAlive"] is True, "Navigating to Support/About triggered a full page reload"
    assert res["supportStep"]["ariaCurrentHref"] == res["supportStep"]["pathname"]
    assert res["supportStep"]["heading"] != ""
    assert res["supportStep"]["heading"] != res["playsStep"]["heading"]

    assert res["statusStep"]["pathname"] in ("/", "/status")
    assert res["statusStep"]["spaAlive"] is True, "Navigating to Status triggered a full page reload"
    assert res["statusStep"]["ariaCurrentHref"] in ("/", "/status")
    assert res["statusStep"]["heading"] != ""
    assert res["statusStep"]["heading"] != res["playsStep"]["heading"]
    assert res["statusStep"]["heading"] != res["supportStep"]["heading"]

    assert res["back1"]["spaAlive"] is True
    assert res["back1"]["pathname"] == res["supportStep"]["pathname"]
    assert res["back1"]["ariaCurrentHref"] == res["supportStep"]["pathname"]
    assert res["back1"]["heading"] == res["supportStep"]["heading"]

    assert res["back2"]["spaAlive"] is True
    assert res["back2"]["pathname"] == "/plays"
    assert res["back2"]["ariaCurrentHref"] == "/plays"
    assert res["back2"]["heading"] == res["playsStep"]["heading"]

    assert res["forward1"]["spaAlive"] is True
    assert res["forward1"]["pathname"] == res["supportStep"]["pathname"]
    assert res["forward1"]["ariaCurrentHref"] == res["supportStep"]["pathname"]
    assert res["forward1"]["heading"] == res["supportStep"]["heading"]


def test_browser_t1_theme_toggle_and_persistence(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """Enforces the single dark Acoustic Noir theme even when OS prefers light."""
    res = pw_worker.call(
        "check_theme_toggle",
        baseUrl=std_server.base_url,
        path="/",
        viewport={"width": 1440, "height": 900},
        colorScheme="light",
    )
    _assert_clean_runtime(res)
    assert res["before"]["theme"] == "dark"
    assert res["before"]["stored"] == "dark"
    assert res["before"]["toggleCount"] == 0
    assert res["before"]["bg"] == "rgb(9, 9, 11)"
    assert res["afterReload"]["theme"] == "dark"
    assert res["afterReload"]["bg"] == res["before"]["bg"]


def test_browser_t1_gateway_qr_and_copy(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    res = pw_worker.call(
        "check_gateway_qr_copy",
        baseUrl=std_server.base_url,
        viewport={"width": 1440, "height": 900},
        pathSecret=False,
    )
    _assert_clean_runtime(res)
    assert res["copyBtnDisabled"] is False
    assert "127.0.0.1" in res["clipboardText"]
    assert res["dialogVisible"] is True
    assert res["qrGraphic"] is not None, "Gateway QR modal did not render valid SVG/Canvas/data-URI img"
    assert res["dialogClosed"] is True


def test_browser_t1_support_qr_copy_and_faq(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    res = pw_worker.call(
        "check_support_controls",
        baseUrl=std_server.base_url,
        viewport={"width": 1440, "height": 900},
    )
    _assert_clean_runtime(res)
    assert res["assetCount"] == 3
    assert res["demoVisible"] is True
    assert "copied" in res["copyTextAfter"].lower()
    assert len(res["clipboardText"]) > 10
    assert res["expandedAfterOpen"] == "true"
    assert res["qrGraphic"] is not None, "Support QR did not render valid SVG/Canvas/data-URI img"
    assert res["expandedAfterClose"] == "false"
    if res["faqCount"] > 0:
        assert res["faqOpened"] is True


def test_browser_t2_public_plays_false_omits_nav_and_returns_404(
    noplays_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """M2/PROJECT.md Contract: PUBLIC_PLAYS=false omits /plays nav link across all pages and returns 404 on /plays."""
    res = pw_worker.call(
        "check_noplays_ui",
        baseUrl=noplays_server.base_url,
        viewport={"width": 1440, "height": 900},
    )
    _assert_clean_runtime(res)
    for row in res["routesChecked"]:
        assert row["navPlaysHref"] == 0, f"Found nav href=/plays on {row['path']}"
        assert row["footerPlaysHref"] == 0, f"Found footer href=/plays on {row['path']}"
        assert row["navPlaysText"] == 0, f"Found Plays nav/footer link on {row['path']}"
    assert res["playsHtmlStatus"] == 404
    assert res["playsApiStatus"] == 404


def test_browser_t2_addon_path_secret_masks_url_and_serves_root_assets(
    secret_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    for route in ("/status", "/plays", "/support"):
        audit = pw_worker.call(
            "audit_route",
            baseUrl=secret_server.base_url,
            path=route,
            viewport={"width": 1440, "height": 900},
            checkScroll=True,
        )
        _assert_clean_runtime(audit)
        _assert_clean_images_and_scroll(audit)

    qr = pw_worker.call(
        "check_gateway_qr_copy",
        baseUrl=secret_server.base_url,
        path="/status",
        viewport={"width": 1440, "height": 900},
        pathSecret=True,
    )
    _assert_clean_runtime(qr)
    assert DEFAULT_PATH_SECRET not in qr["addonUrlText"]
    assert DEFAULT_PATH_SECRET not in qr["dialogText"]
    assert qr["dialogVisible"] is True
    assert qr["dialogClosed"] is True
