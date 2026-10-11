"""Nothing a third party or a bad file does may take the whole app down."""
from __future__ import annotations

from fastapi.testclient import TestClient

from web.app import app

client = TestClient(app)



# --- nothing may take the whole app down -------------------------------------

def test_a_corrupt_data_file_explains_itself_instead_of_a_500(tmp_path, monkeypatch):
    """A half-written config.json used to reach the browser as a traceback, and
    only the dashboard handled it - which is not where people were standing."""
    from agent import paths

    broken = tmp_path / "config.json"
    broken.write_text('{"tone": "casu', encoding="utf-8")   # truncated mid-write
    monkeypatch.setattr(paths, "CONFIG_FILE", broken)

    # Every page that reads the config. /topics reads only topics.json, so it
    # is fine here - which is the right answer, not a gap.
    for url in ("/", "/settings", "/drafts", "/setup/schedule"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert "config.json is not valid JSON" in response.text, url


def test_a_machine_with_no_keyring_says_so(monkeypatch):
    """Headless Linux and WSL have no keyring. Storing a key there used to 500
    right after a successful, billed test call - losing the key silently."""
    from agent.secrets_store import SecretsUnavailable
    from web.routers import wizard

    def no_keyring(name, value):
        raise SecretsUnavailable("This machine has no place to store OPENAI_API_KEY securely.")

    monkeypatch.setattr(wizard, "set_secret", no_keyring)
    monkeypatch.setattr(wizard, "_probe",
                        lambda provider, key, model: {"error": "", "reply": "hi", "models": []})

    response = client.post("/setup/provider", data={"provider": "openai", "api_key": "sk-x"})

    assert response.status_code == 200
    assert "no place to store" in response.text
