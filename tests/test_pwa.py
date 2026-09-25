"""Installable app (user ask, 2026-09-17: "make a progressive app for iOS"; every
screen responsive). The manifest, icons and service worker are served from
/ui; both pages carry the install tags; the worker touches only the shell."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


def _sw_case(name: str) -> dict:
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "sw_harness.mjs"), name], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_manifest_is_served_and_points_inside_its_scope():
    client = TestClient(main.app)
    r = client.get("/ui/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    manifest = r.json()
    assert manifest["display"] == "standalone" and manifest["start_url"].startswith(manifest["scope"] == "/ui/" and "/ui/")
    assert {i["sizes"] for i in manifest["icons"]} >= {"192x192", "512x512"} and any(i.get("purpose") == "maskable" for i in manifest["icons"])
    for icon in manifest["icons"]:
        png = client.get(icon["src"])
        assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:8] == b"\x89PNG\r\n\x1a\n", icon["src"]


def test_the_service_worker_is_served_as_script_without_caching_headers_that_pin_it():
    r = TestClient(main.app).get("/ui/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers.get("cache-control") == "no-cache", "a cached worker would pin an old shell for a day"


@pytest.mark.parametrize("page", ["/ui/", "/ui/admin.html"])
def test_both_pages_carry_the_install_tags(page):
    html = TestClient(main.app).get(page).text
    assert 'rel="manifest" href="/ui/manifest.webmanifest"' in html
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html and 'rel="apple-touch-icon"' in html
    assert "viewport-fit=cover" in html and 'name="theme-color"' in html
    assert "/ui/mobile.css" in html, "the phone/touch layer is loaded last on every screen"


def test_the_chat_page_registers_the_worker_under_the_ui_scope():
    html = (STATIC / "index.html").read_text()
    assert 'navigator.serviceWorker.register("/ui/sw.js",{scope:"/ui/"})' in html
    assert 'id="installCard"' in html and "beforeinstallprompt" in html


def test_the_worker_never_intercepts_api_routes():
    r = _sw_case("api_routes_are_never_intercepted")
    assert r == {"chat": False, "chat_stream": False, "me": False, "billing": False, "readyz": False, "cross_origin": False}, r


def test_the_worker_serves_shell_files_from_cache_and_refreshes_them():
    r = _sw_case("shell_files_are_answered_from_the_cache_and_refreshed")
    assert r["answered"] == "cached:css"


def test_a_navigation_is_network_first_with_the_shell_as_offline_fallback():
    r = _sw_case("a_navigation_prefers_the_network_and_falls_back_to_the_shell")
    assert r["online"] == "https://orbit.example/ui/?source=pwa" and r["offline"] == "cached:shell"


def test_install_precaches_the_shell_the_pages_actually_load():
    r = _sw_case("install_precaches_the_shell_and_activate_drops_old_caches")
    html = (STATIC / "index.html").read_text()
    for sheet in ("/ui/theme.css?v=2", "/ui/chat.css?v=10", "/ui/product.css?v=10", "/ui/mobile.css?v=7"):
        assert sheet in html and sheet in r["precached"], sheet


def test_touch_fields_are_16px_so_ios_does_not_zoom_and_targets_are_finger_sized():
    css = (STATIC / "mobile.css").read_text()
    assert "@media (pointer: coarse)" in css and "font-size: 16px" in css and "min-height: 42px" in css
    assert "env(safe-area-inset-top)" in css and "env(safe-area-inset-bottom)" in css


def test_the_app_height_follows_the_stylesheet_unless_the_keyboard_is_up():
    """Seen on an installed iPhone app: a short visual-viewport reading at
    launch was written into --app-height and never corrected, leaving a dead
    band under the composer. The measured height now applies only while the
    keyboard is up (visual viewport well shorter than the window)."""
    from tests.test_ui_swap_flow import run_case
    r = run_case("app_height_follows_the_stylesheet_unless_the_keyboard_is_up")
    assert r == {"keyboard": "500px", "settled": "100dvh", "toolbars": "100dvh", "unknown": "100dvh"}, r


# --- review of 2026-09-18: two service-worker defects -------------------------------

def test_each_page_keeps_its_own_offline_copy_so_admin_never_replaces_the_chat_shell():
    """PWA-01: every navigation was cached under the one /ui/ key, so after a
    visit to Admin the app opened offline as Admin."""
    r = _sw_case("each_page_keeps_its_own_offline_copy")
    assert r["chatKey"].endswith("/ui/?source=pwa") and r["adminKey"].endswith("/ui/admin.html")
    assert r["offline"] == {"chat": "cached:chat", "admin": "cached:admin", "other": "cached:chat"}, r


def test_an_error_page_never_replaces_a_cached_page():
    r = _sw_case("an_error_page_never_replaces_a_cached_page")
    assert r == {"servedStatus": 502, "chatKey": "cached:chat"}, r


def test_activation_drops_only_orbits_own_older_caches():
    """PWA-02: activation deleted every cache on the origin, another app's included."""
    r = _sw_case("activate_keeps_caches_that_belong_to_other_apps")
    assert r["remaining"] == ["other-app-cache", "workbox-precache"], r
