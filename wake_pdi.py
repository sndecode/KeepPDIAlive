#!/usr/bin/env python3
"""
wake_pdi.py  (v2 - robust + self-diagnosing)
--------------------------------------------
Keeps a ServiceNow PDI alive by signing into the Developer Site, which both
wakes the instance from hibernation and resets the 10-day reclaim timer.

This version:
  * dismisses the cookie-consent banner (it intercepts clicks otherwise)
  * clicks "Sign In" robustly (multiple strategies)
  * screenshots EVERY stage -> debug-*.png
  * dumps every input field's attributes to the log so selectors can be
    confirmed if anything still fails

Env vars (set as GitHub Secrets):
  SN_DEV_USER -> developer.servicenow.com email
  SN_DEV_PASS -> developer.servicenow.com password
"""

import os
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

DEV_PORTAL = "https://developer.servicenow.com/dev.do"
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"


def shot(page, name):
    """Save a screenshot and never let it crash the run."""
    try:
        page.screenshot(path=name, full_page=False)
        print(f"  [screenshot] {name}")
    except Exception as e:
        print(f"  [screenshot failed] {name}: {e}")


def dump_inputs(page, label):
    """Print every input field so we know the real selectors."""
    print(f"--- input fields visible at: {label} ---")
    try:
        for i, el in enumerate(page.query_selector_all("input")):
            try:
                print(
                    f"  input[{i}] "
                    f"type={el.get_attribute('type')!r} "
                    f"name={el.get_attribute('name')!r} "
                    f"id={el.get_attribute('id')!r} "
                    f"placeholder={el.get_attribute('placeholder')!r} "
                    f"visible={el.is_visible()}"
                )
            except Exception:
                pass
    except Exception as e:
        print(f"  (could not enumerate inputs: {e})")
    print("--- end inputs ---")


def try_click(page, selectors, what, timeout=4000):
    """Try a list of selectors; return True on first successful click."""
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=timeout)
            print(f"  clicked {what} via: {sel}")
            return True
        except PWTimeout:
            continue
        except Exception:
            continue
    print(f"  WARN: could not click {what}")
    return False


def is_present(page, selector, timeout=4000):
    """Return True if a selector becomes visible within timeout, else False."""
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def wake():
    user = os.environ.get("SN_DEV_USER")
    password = os.environ.get("SN_DEV_PASS")
    if not user or not password:
        print("ERROR: set SN_DEV_USER and SN_DEV_PASS env vars.")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        # capture any popup window that Sign In might open
        popups = []
        ctx.on("page", lambda pg: popups.append(pg))

        try:
            print("Opening developer portal...")
            page.goto(DEV_PORTAL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            shot(page, "debug-1-landing.png")

            # 1) Kill the cookie banner so it stops eating clicks.
            try_click(
                page,
                ["button:has-text('Accept')", "text=Accept All", "text=Accept"],
                "cookie accept",
            )
            page.wait_for_timeout(1500)

            # 2) Click Sign In (header, top-right).
            try_click(
                page,
                [
                    "a:has-text('Sign In')",
                    "button:has-text('Sign In')",
                    "text=Sign In",
                    "[href*='login']",
                    "[aria-label*='Sign In']",
                ],
                "Sign In",
                timeout=8000,
            )

            # Sign In may redirect in-tab OR open a popup. Resolve which.
            page.wait_for_timeout(6000)
            target = page
            if popups:
                target = popups[-1]
                print(f"  login opened in popup: {target.url}")
                target.wait_for_load_state("domcontentloaded")
            print("  current URL:", target.url)
            shot(target, "debug-2-after-signin.png")
            dump_inputs(target, "after Sign In")

            # 3) Username step (broad selector net).
            user_sel = (
                "input[type='email'], input[name='username'], #username, "
                "input[name='email'], input[id*='user'], input[type='text']"
            )
            target.locator(user_sel).first.fill(user, timeout=20000)
            print("  filled username")
            try_click(
                target,
                ["button:has-text('Next')", "button:has-text('Continue')",
                 "button[type='submit']", "#submitButton", "input[type='submit']"],
                "Next/Continue",
            )
            target.wait_for_timeout(4000)
            shot(target, "debug-3-after-username.png")
            dump_inputs(target, "after username submit")

            # 4) Password step.
            pass_sel = "input[type='password'], input[name='password'], #password"
            target.locator(pass_sel).first.fill(password, timeout=20000)
            print("  filled password")
            try_click(
                target,
                ["button:has-text('Sign In')", "button:has-text('Log in')",
                 "button[type='submit']", "#submitButton", "input[type='submit']"],
                "final submit",
            )

            target.wait_for_timeout(12000)
            print("  post-login URL:", target.url)
            shot(target, "debug-4-final.png")

            # Optional explicit wake button if the portal shows one.
            try_click(
                target,
                ["text=Wake instance", "text=Start instance", "text=Wake"],
                "wake button",
            )
            target.wait_for_timeout(8000)
            shot(target, "debug-5-done.png")

            # 5) VERIFY we are actually logged in. Filling/clicking the login
            # form never throws on bad credentials — ServiceNow just shows an
            # error page — so we must confirm auth or we'd report false success
            # while the PDI quietly gets reclaimed.
            #
            # IMPORTANT: we verify ON the post-login page (no re-navigation).
            # Re-loading dev.do briefly renders a logged-out shell ("Sign In"
            # flashes for a moment before the session resolves), and a waiting
            # check catches that transient and FALSE-FAILS a good login.
            # So: let the page settle, then take an INSTANT snapshot of the
            # current state (no wait_for, which would catch transients).
            print("Verifying authentication...")
            try:
                target.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            target.wait_for_timeout(5000)
            shot(target, "debug-6-verify.png")

            url = (target.url or "").lower()

            def visible_now(sel):
                """Current visibility only — does NOT wait, so no transients."""
                try:
                    return target.locator(sel).first.is_visible()
                except Exception:
                    return False

            # Failure signals (instant):
            signed_out = visible_now("a:has-text('Sign In')")
            on_login_page = ("signon" in url) or ("/login" in url) or ("ssologin" in url)
            # Sanity: confirm the portal actually loaded (true in both states).
            portal_loaded = visible_now("text=Developer") or ("developer.servicenow.com" in url)
            # Bonus positive marker — diagnostics only, NOT required.
            authed_marker = visible_now("text=Manage my instance") or visible_now("text=Start building")

            print(f"  url={url}")
            print(f"  signed_out={signed_out}  on_login_page={on_login_page}  "
                  f"portal_loaded={portal_loaded}  authed_marker={authed_marker}")

            if signed_out or on_login_page:
                raise RuntimeError(
                    "Login verification FAILED — still on a login page / 'Sign In' "
                    "visible (wrong credentials, MFA, or a redirect)."
                )
            if not portal_loaded:
                raise RuntimeError(
                    "Login verification FAILED — the Developer Portal did not load; "
                    "cannot confirm authentication."
                )

            print("Done. Login verified, PDI timer reset.")
        except Exception as e:
            print("FAILED:", repr(e))
            shot(page, "failure.png")
            for i, pg in enumerate(popups):
                shot(pg, f"failure-popup-{i}.png")
            browser.close()
            sys.exit(2)

        browser.close()


if __name__ == "__main__":
    wake()
