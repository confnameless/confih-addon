"""Tier 3 (Pairwise Combinatorial Matrix) & Tier 4 (Real-World End-User Scenarios) E2E Tests."""

from __future__ import annotations

import httpx
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


def _assert_scroll_clean(scroll: dict, label: str = "") -> None:
    assert scroll["overflowFailures"] == [], f"{label} overflow: {scroll['overflowFailures']}"
    assert scroll["strandedFailures"] == [], f"{label} stranded elements: {scroll['strandedFailures']}"


# ── Tier 3: 8-Cell Pairwise Combinatorial Matrix ────────────────────────────


@pytest.mark.parametrize(
    ("cell_id", "viewport", "motion", "theme", "entry_route", "server_kind"),
    [
        ("P1", {"width": 1440, "height": 900}, "no-preference", "dark", "/", "std"),
        ("P2", {"width": 390, "height": 844}, "reduce", "light", "/", "std"),
        ("P3", {"width": 1440, "height": 900}, "reduce", "light", "/plays", "std"),
        ("P4", {"width": 390, "height": 844}, "no-preference", "dark", "/plays", "std"),
        ("P5", {"width": 1440, "height": 900}, "no-preference", "light", "/support", "std"),
        ("P6", {"width": 390, "height": 844}, "reduce", "dark", "/support", "std"),
        ("P7", {"width": 1440, "height": 900}, "no-preference", "dark", "/", "noplays"),
        ("P8", {"width": 390, "height": 844}, "no-preference", "light", "/status", "secret"),
    ],
    ids=["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"],
)
def test_tier3_pairwise_matrix(
    cell_id: str,
    viewport: dict[str, int],
    motion: str,
    theme: str,
    entry_route: str,
    server_kind: str,
    std_server: IsolatedUvicornServer,
    noplays_server: IsolatedUvicornServer,
    secret_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    servers = {
        "std": std_server,
        "noplays": noplays_server,
        "secret": secret_server,
    }
    srv = servers[server_kind]
    res = pw_worker.call(
        "audit_route",
        baseUrl=srv.base_url,
        path=entry_route,
        viewport=viewport,
        colorScheme=theme,
        reducedMotion=motion,
        checkDrag=(cell_id == "P1"),
        checkScroll=True,
    )
    _assert_clean_runtime(res)
    assert res["status"] == 200
    assert res["images"]["broken"] == [], f"{cell_id} broken images: {res['images']['broken']}"
    _assert_scroll_clean(res["scrollAudit"], label=cell_id)

    if cell_id == "P1":
        assert res["spatial"]["active3d"] is True
        assert res["spatial"]["dragChanged"] is True
        assert res["spatial"]["dragLayoutStable"] is True
    elif motion == "reduce":
        assert res["spatial"]["reducedTransitionsOk"]["matches"] is True
        assert res["spatial"]["reducedTransitionsOk"]["slowEls"] == []
        if res["spatial"]["hasCanvas"]:
            assert res["spatial"]["reducedStatic"] is True

    if entry_route == "/plays":
        ctrl = pw_worker.call(
            "check_plays_controls",
            baseUrl=srv.base_url,
            viewport=viewport,
            colorScheme=theme,
            reducedMotion=motion,
        )
        _assert_clean_runtime(ctrl)
        assert ctrl["counts"]["topCount"] >= 1
        assert ctrl["pressedOn"] == "true"

    if entry_route == "/support":
        sup = pw_worker.call(
            "check_support_controls",
            baseUrl=srv.base_url,
            viewport=viewport,
            colorScheme=theme,
            reducedMotion=motion,
        )
        _assert_clean_runtime(sup)
        assert sup["assetCount"] == 3
        assert sup["qrGraphic"] is not None

    if server_kind == "noplays":
        np_res = pw_worker.call("check_noplays_ui", baseUrl=srv.base_url, viewport=viewport)
        _assert_clean_runtime(np_res)
        assert np_res["playsHtmlStatus"] == 404
        assert np_res["playsApiStatus"] == 404

    if server_kind == "secret":
        qr = pw_worker.call(
            "check_gateway_qr_copy",
            baseUrl=srv.base_url,
            path="/status",
            viewport=viewport,
            pathSecret=True,
        )
        _assert_clean_runtime(qr)
        assert DEFAULT_PATH_SECRET not in qr["addonUrlText"]
        assert DEFAULT_PATH_SECRET not in qr["dialogText"]


# ── Tier 4: 5 Real-World End-User Scenarios ─────────────────────────────────


def test_tier4_e1_desktop_listener_setup(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """Scenario E1: First-time desktop listener inspects 3D stage, toggles theme, copies URL, checks QR, navigates to Support & Back."""
    res = pw_worker.call(
        "run_journey",
        scenario="E1_desktop_listener_setup",
        baseUrl=std_server.base_url,
        viewport={"width": 1440, "height": 900},
        colorScheme="dark",
        reducedMotion="no-preference",
    )
    _assert_clean_runtime(res)
    assert res["spatial"]["active3d"] is True
    assert res["spatial"]["dragChanged"] is True
    assert res["spatial"]["dragLayoutStable"] is True
    assert res["themeBefore"] == "dark"
    assert res["themeAfter"] == "dark"
    assert "127.0.0.1" in res["clipboard"]
    assert res["qrGraphic"] is not None
    assert res["spaAliveOnSupport"] is True
    assert res["faqOpened"] is True
    assert res["backState"]["pathname"] in ("/", "/status")
    assert res["backState"]["theme"] == res["themeAfter"]
    assert res["backState"]["spaAlive"] is True
    _assert_scroll_clean(res["supportScroll"], "E1_support")
    _assert_scroll_clean(res["statusScroll"], "E1_status")


def test_tier4_e2_live_audio_ledger_exploration(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """Scenario E2: Desktop listener deep-links /plays, verifies /art/ covers, scrubs 24h chart, filters tier, refreshes, scrolls."""
    res = pw_worker.call(
        "run_journey",
        scenario="E2_live_audio_ledger",
        baseUrl=std_server.base_url,
        viewport={"width": 1440, "height": 900},
        colorScheme="dark",
        reducedMotion="no-preference",
    )
    _assert_clean_runtime(res)
    assert res["images"]["broken"] == []
    assert res["topCount"] >= 4
    assert res["recentCount"] >= 4
    assert res["readout1"] != res["readout0"]
    assert res["readoutArrow"] != res["readout0"]
    assert res["readoutArrow"] != res["readout1"]
    assert res["tierPressed"] == "true"
    assert res["refreshOk"] is True
    _assert_scroll_clean(res["scroll"], "E2_plays")


def test_tier4_e3_mobile_commuter_inspection_and_support(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
) -> None:
    """Scenario E3: Mobile 390x844 visitor opens /support, inspects QR & Copy, navigates Status -> Plays -> Back."""
    res = pw_worker.call(
        "run_journey",
        scenario="E3_mobile_commuter_support",
        baseUrl=std_server.base_url,
        viewport={"width": 390, "height": 844},
        colorScheme="dark",
        reducedMotion="no-preference",
    )
    _assert_clean_runtime(res)
    assert res["assetCount"] == 3
    assert res["qrGraphic"] is not None
    assert res["backState"]["pathname"] in ("/", "/status")
    assert res["backState"]["spaAlive"] is True
    _assert_scroll_clean(res["supportScroll"], "E3_support")
    _assert_scroll_clean(res["statusScroll"], "E3_status")
    _assert_scroll_clean(res["playsScroll"], "E3_plays")


@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 1440, "height": 900},
        {"width": 390, "height": 844},
    ],
    ids=["desktop_1440x900", "mobile_390x844"],
)
def test_tier4_e4_motion_sensitive_accessibility_journey(
    std_server: IsolatedUvicornServer,
    pw_worker: PlaywrightWorker,
    viewport: dict[str, int],
) -> None:
    """Scenario E4: Reduced-motion visitor verifies static 3D stage, zero transitions, QR, SPA nav, and bidirectional scroll."""
    res = pw_worker.call(
        "run_journey",
        scenario="E4_reduced_motion_accessibility",
        baseUrl=std_server.base_url,
        viewport=viewport,
        colorScheme="light",
        reducedMotion="reduce",
    )
    _assert_clean_runtime(res)
    assert res["spatial"]["active3d"] is True
    assert res["spatial"]["reducedTransitionsOk"]["matches"] is True
    assert res["spatial"]["reducedTransitionsOk"]["slowEls"] == []
    if res["spatial"]["hasCanvas"]:
        assert res["spatial"]["canvasStats"]["distinctColors"] >= 4
        assert res["spatial"]["reducedStatic"] is True
    assert res["qrGraphic"] is not None
    assert res["backState"]["pathname"] == "/plays"
    assert res["spaAliveAfterBack"] is True
    _assert_scroll_clean(res["statusScroll"], "E4_status")
    _assert_scroll_clean(res["playsScroll"], "E4_plays")
    _assert_scroll_clean(res["supportScroll"], "E4_support")


def test_tier4_e5_hardened_private_deployment_journey(pw_worker: PlaywrightWorker) -> None:
    """Scenario E5: Combined PUBLIC_PLAYS=false + ADDON_PATH_SECRET hides /plays, isolates BitChord discovery, masks secret."""
    with IsolatedUvicornServer(
        {
            "PUBLIC_PLAYS": "false",
            "DONATE_DEMO": "false",
            "ADDON_PATH_SECRET": DEFAULT_PATH_SECRET,
            "PUBLIC_BASE_URL": f"https://gw.private.example/s/{DEFAULT_PATH_SECRET}",
        },
        seed_plays=False,
        seed_artwork=False,
        seed_stats=True,
    ) as srv:
        with httpx.Client(base_url=srv.base_url, timeout=5.0) as client:
            assert client.get("/").status_code == 404
            assert client.get("/manifest.json").status_code == 404
            assert client.get("/plays").status_code == 404
            assert client.get("/api/plays").status_code == 404
            root_json = client.get(
                f"/s/{DEFAULT_PATH_SECRET}/",
                headers={"User-Agent": "BitChord/2.0"},
            )
            assert root_json.status_code == 200
            assert root_json.json()["manifest"] == f"/s/{DEFAULT_PATH_SECRET}/manifest.json"
            assert client.get(f"/s/{DEFAULT_PATH_SECRET}/manifest.json").status_code == 200

        np_ui = pw_worker.call(
            "check_noplays_ui",
            baseUrl=srv.base_url,
            routes=["/status", "/about", "/support"],
            viewport={"width": 1440, "height": 900},
        )
        _assert_clean_runtime(np_ui)
        for row in np_ui["routesChecked"]:
            assert row["navPlaysHref"] == 0
            assert row["footerPlaysHref"] == 0

        res = pw_worker.call(
            "run_journey",
            scenario="E5_hardened_private_deployment",
            baseUrl=srv.base_url,
            viewport={"width": 1440, "height": 900},
        )
        _assert_clean_runtime(res)
        assert DEFAULT_PATH_SECRET not in res["addonUrlText"]
        assert DEFAULT_PATH_SECRET not in res["modalText"]
        _assert_scroll_clean(res["statusScroll"], "E5_status")
        _assert_scroll_clean(res["supportScroll"], "E5_support")
