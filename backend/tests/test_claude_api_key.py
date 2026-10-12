"""An Anthropic Console API key runs the built-in Claude provider.

Owners without a Claude subscription can save a pay-per-use key from
Settings. These tests pin the sealed entry path, owner-only storage, the
fixed precedence (a usable subscription sign-in always wins), the single
injection point, removal, and the billing/auth error guidance. No test calls
Anthropic: every network seam is faked.
"""
import asyncio
import json
import stat
import subprocess
import time
from types import SimpleNamespace

import pytest

from app import auth as auth_module
from app import claude_api_key, provider_usage, providers
from app.chat_writer import StartTurn, get_writer
from app.config import get_settings
from app.provider_errors import ProviderErrorKind, classify_provider_error

KEY = "sk-ant-api03-" + "A" * 40 + "_synthetic-test-key"
OTHER_KEY = "sk-ant-api03-" + "B" * 40 + "_synthetic-other-key"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
  root = tmp_path / "data"
  (root / "cli-auth" / "claude").mkdir(parents=True)
  monkeypatch.setattr(get_settings(), "data_dir", str(root))
  return root


def _sign_in(data_dir, *, usable=True):
  creds = data_dir / "cli-auth" / "claude" / ".credentials.json"
  creds.write_text(json.dumps({"claudeAiOauth": {
    "accessToken": "synthetic-access",
    "expiresAt": int(time.time() * 1000) + (600_000 if usable else -600_000),
  }}))
  return creds


@pytest.fixture
def anthropic_accepts(monkeypatch):
  checked = []

  async def verify(key):
    checked.append(key)

  monkeypatch.setattr(claude_api_key, "verify_api_key", verify)
  return checked


# ── storage ──────────────────────────────────────────────────────────────


def test_saved_key_is_owner_only_and_removable(data_dir):
  claude_api_key.save_api_key(str(data_dir), KEY)
  path = claude_api_key.api_key_path(str(data_dir))
  assert stat.S_IMODE(path.stat().st_mode) == 0o600
  assert claude_api_key.read_api_key(str(data_dir)) == KEY

  claude_api_key.remove_api_key(str(data_dir))
  claude_api_key.remove_api_key(str(data_dir))  # idempotent
  assert claude_api_key.read_api_key(str(data_dir)) is None


def test_a_damaged_key_file_asks_the_owner_to_enter_it_again(data_dir):
  claude_api_key.api_key_path(str(data_dir)).write_text("garbage")
  with pytest.raises(claude_api_key.ClaudeApiKeyUnreadable):
    claude_api_key.read_api_key(str(data_dir))
  assert providers.claude_auth_method(str(data_dir)) is None
  error = providers.ClaudeProvider().check_auth(str(data_dir))
  assert "enter it again" in error


@pytest.mark.parametrize("value", [
  "", "   ", "not-a-key", "sk-ant-admin01-" + "A" * 40, "sk-ant-api03-short",
  "sk-ant-api03-" + "A" * 40 + " with space",
])
def test_malformed_values_are_refused_without_echoing_them(value):
  with pytest.raises(ValueError) as error:
    claude_api_key.normalize_api_key(value)
  if value.strip():
    assert value.strip() not in str(error.value)


def test_a_signin_token_is_redirected_to_the_signin_flow():
  with pytest.raises(ValueError, match="Sign in with Claude"):
    claude_api_key.normalize_api_key("sk-ant-oat01-" + "A" * 40)


def test_surrounding_whitespace_from_a_paste_is_ignored():
  assert claude_api_key.normalize_api_key(f"  {KEY}\n") == KEY


# ── precedence and the pre-turn check ────────────────────────────────────


def test_api_key_alone_authenticates_claude(data_dir):
  claude_api_key.save_api_key(str(data_dir), KEY)
  assert providers.claude_auth_method(str(data_dir)) == "api_key"
  assert providers.ClaudeProvider().check_auth(str(data_dir)) is None


def test_usable_subscription_signin_takes_precedence_over_a_saved_key(data_dir):
  _sign_in(data_dir)
  claude_api_key.save_api_key(str(data_dir), KEY)
  assert providers.claude_auth_method(str(data_dir)) == "subscription"


def test_saved_key_takes_over_when_the_signin_is_expired(data_dir):
  _sign_in(data_dir, usable=False)
  claude_api_key.save_api_key(str(data_dir), KEY)
  assert providers.claude_auth_method(str(data_dir)) == "api_key"
  assert providers.ClaudeProvider().check_auth(str(data_dir)) is None


def test_unconnected_claude_names_both_ways_in(data_dir):
  error = providers.ClaudeProvider().check_auth(str(data_dir))
  assert "sign in with Claude" in error
  assert "API key" in error


def test_pre_turn_refresh_is_skipped_on_an_api_key(data_dir, monkeypatch):
  claude_api_key.save_api_key(str(data_dir), KEY)

  async def must_not_refresh(_data_dir):
    raise AssertionError("an API-key turn must not touch the OAuth refresh")

  monkeypatch.setattr(providers, "claude_access_token", must_not_refresh)
  asyncio.run(providers.ClaudeProvider().ensure_auth(str(data_dir)))


# ── environment injection ────────────────────────────────────────────────


def test_saved_key_never_enters_the_environment_children_inherit(data_dir):
  """The CLI hands its env to Bash commands, MCP servers and package scripts;
  an `env` there must not print the key into the transcript."""
  claude_api_key.save_api_key(str(data_dir), KEY)
  env = providers.ClaudeProvider().build_env({}, str(data_dir), "chat-1")
  assert KEY not in json.dumps(env)
  assert env["ANTHROPIC_API_KEY"] == ""
  assert env["ANTHROPIC_AUTH_TOKEN"] == ""
  # Sessions live beside the sign-in so resume works in either mode.
  assert env["CLAUDE_CONFIG_DIR"] == str(data_dir / "cli-auth" / "claude")


def test_key_reaches_the_cli_only_through_its_api_key_helper(data_dir):
  claude_api_key.save_api_key(str(data_dir), KEY)
  settings = json.loads(
    providers.claude_cli_settings(str(data_dir), {"disableWorkflows": True})
  )
  assert settings["disableWorkflows"] is True
  assert KEY not in json.dumps(settings)
  helper = subprocess.run(
    settings["apiKeyHelper"], shell=True, capture_output=True, text=True,
    check=True, env={"PATH": "/usr/bin:/bin"},
  )
  assert helper.stdout.strip() == KEY


def test_signed_in_runs_get_no_api_key_helper(data_dir):
  """The CLI ranks apiKeyHelper above the sign-in, so it must be absent."""
  _sign_in(data_dir)
  claude_api_key.save_api_key(str(data_dir), KEY)
  assert json.loads(providers.claude_cli_settings(str(data_dir), {})) == {}
  assert json.loads(providers.claude_cli_settings(str(data_dir), {"x": 1})) == {"x": 1}


def test_ambient_key_can_never_override_a_signed_in_account(data_dir, monkeypatch):
  """The SDK merges os.environ under the provider env; blanking wins."""
  _sign_in(data_dir)
  monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-ambient")
  monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token")
  env = providers.ClaudeProvider().build_env({}, str(data_dir))
  assert env["ANTHROPIC_API_KEY"] == ""
  assert env["ANTHROPIC_AUTH_TOKEN"] == ""


def test_saved_key_reaches_no_other_provider_environment(data_dir):
  claude_api_key.save_api_key(str(data_dir), KEY)
  (data_dir / "cli-auth" / "codex").mkdir()
  (data_dir / "cli-auth" / "codex" / "auth.json").write_text("{}")
  codex_env = providers.CodexProvider().build_env({}, str(data_dir), "chat-1")
  assert KEY not in json.dumps(codex_env)

  app_provider = providers.AppModelProvider(7, {
    "name": "Example", "base_url": "https://models.example.com/v1",
    "secret_name": "api_key", "default_model": "m",
    "models": [{"id": "m", "label": "M"}],
  })
  secret = data_dir / "app-secrets" / "7" / "api_key"
  secret.parent.mkdir(parents=True)
  from app.app_secret_crypto import app_secret_fernet
  secret.write_bytes(app_secret_fernet().encrypt(b"app-key"))
  # A Claude env handed on as the base must still be stripped.
  claude_env = providers.ClaudeProvider().build_env({}, str(data_dir))
  app_env = app_provider.build_env(claude_env, str(data_dir), "chat-1")
  assert app_env["ANTHROPIC_API_KEY"] == ""


# ── model listing and usage ──────────────────────────────────────────────


def test_model_listing_authenticates_with_the_api_key(data_dir, monkeypatch):
  claude_api_key.save_api_key(str(data_dir), KEY)
  seen = {}

  class FakeClient:
    def __init__(self, **_kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_exc):
      return False

    async def get(self, url, headers):
      seen.update(url=url, headers=headers)
      return SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": [{"id": "claude-opus-4-8", "display_name": "Opus"}]},
      )

  import httpx
  monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

  async def no_oauth(_data_dir):
    raise AssertionError("API-key listing must not read the OAuth token")

  monkeypatch.setattr(providers, "claude_access_token", no_oauth)
  models = asyncio.run(providers._fetch_claude_models(str(data_dir)))
  assert models == [{"id": "claude-opus-4-8", "label": "Opus"}]
  assert seen["headers"]["x-api-key"] == KEY
  assert "Authorization" not in seen["headers"]


def test_api_key_usage_reads_api_billing_without_contacting_anthropic(data_dir, monkeypatch):
  claude_api_key.save_api_key(str(data_dir), KEY)

  async def no_fetch(_data_dir):
    raise AssertionError("API billing has no plan windows to fetch")

  monkeypatch.setattr(provider_usage, "_fetch_claude_usage", no_fetch)
  snapshot = asyncio.run(provider_usage._provider_snapshot("claude", str(data_dir)))
  assert snapshot["plan_label"] == "API billing"
  assert provider_usage.configured_plan_labels(str(data_dir))["claude"] == "API billing"


# ── routes ───────────────────────────────────────────────────────────────

URL = "/api/auth/provider/claude/api-key"


def test_owner_saves_a_checked_key_and_status_reports_it(client, auth, data_dir, anthropic_accepts):
  response = client.put(URL, headers=auth, json={"api_key": f" {KEY} "})
  assert response.status_code == 200, response.text
  assert response.json() == {"ok": True, "auth_method": "api_key"}
  assert anthropic_accepts == [KEY]
  assert claude_api_key.read_api_key(str(data_dir)) == KEY

  status = client.get("/api/auth/providers/status", headers=auth).json()["claude"]
  assert status["configured"] is True
  assert status["auth_method"] == "api_key"
  assert status["api_key_saved"] is True


def test_rejected_or_malformed_keys_are_not_saved_or_echoed(client, auth, data_dir, monkeypatch):
  bad = "sk-ant-api03-nope"
  response = client.put(URL, headers=auth, json={"api_key": bad})
  assert response.status_code == 400
  assert bad not in response.text

  async def rejected(_key):
    raise ValueError("Anthropic rejected this API key.")

  monkeypatch.setattr(claude_api_key, "verify_api_key", rejected)
  response = client.put(URL, headers=auth, json={"api_key": KEY})
  assert response.status_code == 400
  assert "rejected" in response.json()["detail"]
  assert KEY not in response.text
  assert not claude_api_key.api_key_path(str(data_dir)).exists()


def test_unreachable_anthropic_saves_nothing(client, auth, data_dir, monkeypatch):
  async def unreachable(_key):
    raise ConnectionError("down")

  monkeypatch.setattr(claude_api_key, "verify_api_key", unreachable)
  response = client.put(URL, headers=auth, json={"api_key": KEY})
  assert response.status_code == 502
  assert not claude_api_key.api_key_path(str(data_dir)).exists()


def test_agents_and_cross_site_pages_cannot_supply_the_key(client, auth, chat, data_dir, anthropic_accepts):
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token="key-run",
    user_msg={"role": "user", "content": "hi", "ts": 1}, title_source="hi",
  )).result(timeout=5)
  agent = auth_module.create_agent_token(chat.id, "test", 0, run_id="key-run")
  agent_headers = {"Authorization": f"Bearer {agent}"}
  assert client.put(URL, headers=agent_headers, json={"api_key": KEY}).status_code == 403
  assert client.put(URL, json={"api_key": KEY}).status_code in (401, 403)
  cross_site = {**auth, "Sec-Fetch-Site": "cross-site"}
  assert client.put(URL, headers=cross_site, json={"api_key": KEY}).status_code == 403
  assert anthropic_accepts == []
  assert not claude_api_key.api_key_path(str(data_dir)).exists()


def test_removing_the_key_leaves_a_signin_in_place(client, auth, data_dir, anthropic_accepts):
  creds = _sign_in(data_dir)
  assert client.put(URL, headers=auth, json={"api_key": KEY}).json()["auth_method"] == "subscription"
  status = client.get("/api/auth/providers/status", headers=auth).json()["claude"]
  assert status["auth_method"] == "subscription"
  assert status["api_key_saved"] is True

  response = client.delete(URL, headers=auth)
  assert response.status_code == 200
  assert response.json()["auth_method"] == "subscription"
  assert not claude_api_key.api_key_path(str(data_dir)).exists()
  assert "claudeAiOauth" in json.loads(creds.read_text())


def test_removing_the_only_credential_disconnects_claude(client, auth, data_dir, anthropic_accepts):
  client.put(URL, headers=auth, json={"api_key": KEY})
  assert client.delete(URL, headers=auth).json()["auth_method"] is None
  status = client.get("/api/auth/providers/status", headers=auth).json()["claude"]
  assert status["configured"] is False
  assert status["api_key_saved"] is False


def test_disconnecting_claude_removes_the_key_too(client, auth, data_dir, anthropic_accepts):
  creds = _sign_in(data_dir)
  client.put(URL, headers=auth, json={"api_key": KEY})
  assert client.post("/api/auth/provider/claude/disconnect", headers=auth).status_code == 200
  assert not claude_api_key.api_key_path(str(data_dir)).exists()
  assert "claudeAiOauth" not in json.loads(creds.read_text())
  assert providers.ClaudeProvider().check_auth(str(data_dir)) is not None


def test_replacing_the_key_uses_the_new_one(client, auth, data_dir, anthropic_accepts):
  client.put(URL, headers=auth, json={"api_key": KEY})
  client.put(URL, headers=auth, json={"api_key": OTHER_KEY})
  assert claude_api_key.read_api_key(str(data_dir)) == OTHER_KEY


# ── billing and auth errors ──────────────────────────────────────────────


class _Sink:
  chat_id = "billing-chat"
  run_token = None

  def __init__(self):
    self.events = []

  def publish(self, event):
    self.events.append(event)


def test_anthropic_credit_refusal_becomes_a_resumable_credits_pause(data_dir):
  from app.chat import _park_exit
  claude_api_key.save_api_key(str(data_dir), KEY)
  sink = _Sink()
  text = (
    'API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
    '"message":"Your credit balance is too low to access the Anthropic API. '
    'Please go to Plans & Billing to upgrade or purchase credits."}}'
  )
  result = _park_exit(sink, {"api_error_status": 400}, text, provider_id="claude")
  assert result == {"parked": False}
  [event] = sink.events
  assert event["pause"] == {"kind": "credits", "provider": "claude"}
  assert event["resumable"] is True


def test_rejected_api_key_tells_the_owner_where_to_replace_it(data_dir):
  from app.chat import _park_exit
  claude_api_key.save_api_key(str(data_dir), KEY)
  sink = _Sink()
  _park_exit(sink, {}, "Invalid API key · Please run /login", provider_id="claude")
  [event] = sink.events
  assert event["type"] == "error"
  assert "Replace it in Settings" in event["message"]


def test_cli_invalid_api_key_wording_classifies_as_auth():
  assert classify_provider_error("Invalid API key · Please run /login") is ProviderErrorKind.AUTH
  assert classify_provider_error("invalid x-api-key") is ProviderErrorKind.AUTH


def test_subscriber_credit_refusal_is_not_sent_to_console_billing(data_dir):
  from app.chat import _park_exit
  _sign_in(data_dir)
  claude_api_key.save_api_key(str(data_dir), KEY)
  sink = _Sink()
  _park_exit(sink, {}, "Credit balance is too low", provider_id="claude")
  [event] = sink.events
  assert "pause" not in event
  assert event["message"] == "Credit balance is too low"


# ── review hardening ─────────────────────────────────────────────────────


def test_disconnect_during_a_key_check_cannot_be_undone_by_that_save(data_dir, monkeypatch):
  from app.routes import auth as routes
  monkeypatch.setattr(routes, "_provider_login_locks", {"claude": asyncio.Lock(), "codex": asyncio.Lock()})
  monkeypatch.setattr(providers, "_claude_refresh_lock", asyncio.Lock())

  async def scenario():
    checking, release = asyncio.Event(), asyncio.Event()

    async def slow_verify(_key):
      checking.set()
      await release.wait()

    monkeypatch.setattr(claude_api_key, "verify_api_key", slow_verify)
    saving = asyncio.create_task(routes._save_claude_api_key(str(data_dir), KEY))
    await checking.wait()
    disconnecting = asyncio.create_task(providers_disconnect())
    await asyncio.sleep(0.05)
    assert not disconnecting.done()
    release.set()
    await saving
    await disconnecting

  async def providers_disconnect():
    async with routes._provider_login_locks["claude"]:
      await providers.disconnect_provider(str(data_dir), "claude")

  asyncio.run(scenario())
  assert not claude_api_key.api_key_path(str(data_dir)).exists()
  assert providers.claude_auth_method(str(data_dir)) is None


@pytest.mark.parametrize("status, outcome", [
  (200, None), (302, ConnectionError), (307, ConnectionError),
  (401, ValueError), (500, ConnectionError),
])
def test_only_a_success_status_proves_the_key(monkeypatch, status, outcome):
  import httpx
  real_client = httpx.AsyncClient
  seen = {}

  def client(**kwargs):
    seen.update(kwargs)
    return real_client(transport=httpx.MockTransport(
      lambda request: httpx.Response(status, headers={"location": "https://elsewhere.invalid/"}),
    ), **kwargs)

  monkeypatch.setattr(httpx, "AsyncClient", client)
  if outcome is None:
    asyncio.run(claude_api_key.verify_api_key(KEY))
  else:
    with pytest.raises(outcome):
      asyncio.run(claude_api_key.verify_api_key(KEY))
  assert seen["follow_redirects"] is False
