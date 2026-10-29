"""Dashboard HTTPS wiring (2026-10-29).

b69f9fa moved uvicorn to 127.0.0.1 assuming a reverse proxy that did not exist yet,
so the dashboard became unreachable. These tests lock the pieces that make it
reachable again: the proxy points at the loopback port, deploy runs the proxy setup
without letting it block bot deploys, and OAuth settings are read from either
Variables or Secrets.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
SETUP = (ROOT / "deployment" / "setup_https.sh").read_text(encoding="utf-8")
DEPLOY = (ROOT / "deployment" / "deploy.sh").read_text(encoding="utf-8")


def test_dashboard_stays_on_loopback():
    assert "--host 127.0.0.1 --port 8501" in DEPLOY
    assert "--host 0.0.0.0" not in DEPLOY


def test_proxy_targets_loopback_dashboard():
    assert 'UPSTREAM="127.0.0.1:8501"' in SETUP
    assert "reverse_proxy $UPSTREAM" in SETUP


def test_proxy_opens_host_firewall_for_http_and_https():
    assert "for PORT in 80 443" in SETUP


def test_deploy_runs_proxy_setup_non_fatally():
    assert "bash deployment/setup_https.sh" in WORKFLOW
    line = next(l for l in WORKFLOW.splitlines() if "bash deployment/setup_https.sh" in l)
    assert line.rstrip().endswith("\\")  # continued by `|| echo ...`
    assert "|| echo \"⚠️ HTTPS proxy setup failed" in WORKFLOW


def test_oauth_settings_read_from_vars_or_secrets():
    for key in ("ALLOWED_EMAILS", "OAUTH_REDIRECT_URI", "DASHBOARD_COOKIE_SECURE"):
        assert f"vars.{key} || secrets.{key}" in WORKFLOW, key


def test_redirect_uri_defaults_to_https_domain():
    assert 'OAUTH_REDIRECT_URI="https://$DASHBOARD_DOMAIN/auth/callback"' in WORKFLOW
    assert "DASHBOARD_DOMAIN" in WORKFLOW.split("envs:")[1].splitlines()[0]
