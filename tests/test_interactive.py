"""Interactive mode tests. Uses monkeypatch for stdin mocking."""
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from onshape_export_tool import (
    prompt_secrets,
    prompt_document_config,
    interactive_select,
    get_or_prompt_secrets,
    load_secrets,
)


class TestPromptSecrets:
    def test_prompts_and_returns_secrets(self, monkeypatch):
        inputs = iter(["my_access_key", "my_secret_key"])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))
        monkeypatch.setattr('getpass.getpass', lambda _: next(inputs))
        
        result = prompt_secrets()
        assert result['access_key'] == "my_access_key"
        assert result['secret_key'] == "my_secret_key"
    
    def test_strips_whitespace(self, monkeypatch):
        inputs = iter(["  spaced_key  ", "  secret  "])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))
        monkeypatch.setattr('getpass.getpass', lambda _: next(inputs))
        
        result = prompt_secrets()
        assert result['access_key'] == "spaced_key"
        assert result['secret_key'] == "secret"
    
    def test_retries_on_unicode_error(self, monkeypatch):
        """Handles clipboard encoding issues gracefully."""
        call_count = [0]
        
        def mock_input(prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                raise UnicodeDecodeError('utf-8', b'', 0, 1, 'test')
            return "valid_key"
        
        monkeypatch.setattr('builtins.input', mock_input)
        monkeypatch.setattr('getpass.getpass', lambda _: "secret")
        
        result = prompt_secrets()
        assert result['access_key'] == "valid_key"
        assert call_count[0] == 2


class TestPromptDocumentConfig:
    def test_prompts_for_ids(self, monkeypatch):
        inputs = iter(["doc123", "ws456"])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))
        
        did, wid = prompt_document_config()
        assert did == "doc123"
        assert wid == "ws456"


class TestInteractiveSelect:
    def test_selects_item_by_number(self, monkeypatch, capsys):
        items = [{'id': '1', 'name': 'First'}, {'id': '2', 'name': 'Second'}]
        monkeypatch.setattr('builtins.input', lambda _: "2")
        
        result = interactive_select(items, "Choose:", lambda x: x['name'])
        assert result['id'] == '2'
    
    def test_returns_none_on_cancel(self, monkeypatch, capsys):
        items = [{'id': '1', 'name': 'First'}]
        monkeypatch.setattr('builtins.input', lambda _: "0")
        
        assert interactive_select(items, "Choose:", lambda x: x['name']) is None
    
    def test_returns_none_for_empty_list(self, capsys):
        result = interactive_select([], "Choose:", lambda x: x['name'])
        assert result is None
        assert "No items available" in capsys.readouterr().out
    
    def test_reprompts_on_invalid_input(self, monkeypatch, capsys):
        """Invalid then valid input should eventually succeed."""
        items = [{'id': '1', 'name': 'First'}]
        inputs = iter(["99", "abc", "1"])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))
        
        result = interactive_select(items, "Choose:", lambda x: x['name'])
        assert result['id'] == '1'


class TestGetOrPromptSecrets:
    def test_loads_existing_secrets(self, tmp_path):
        secrets_file = tmp_path / ".secrets"
        secrets_file.write_text('{"accessKey": "key1", "secretKey": "secret1"}')
        
        result = get_or_prompt_secrets(secrets_file)
        assert result['access_key'] == "key1"
        assert result['secret_key'] == "secret1"
    
    def test_prompts_if_no_file(self, tmp_path, monkeypatch):
        secrets_file = tmp_path / ".secrets"
        inputs = iter(["new_key", "new_secret", "n"])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))
        monkeypatch.setattr('getpass.getpass', lambda _: next(inputs))
        
        result = get_or_prompt_secrets(secrets_file)
        assert result['access_key'] == "new_key"
    
    def test_offers_to_save(self, tmp_path, monkeypatch):
        """User can opt to persist secrets (encrypted) for future runs."""
        import onshape_export_tool
        secrets_file = tmp_path / ".secrets"
        
        # Mock password cache to avoid encryption password prompt
        monkeypatch.setattr(onshape_export_tool, '_cached_password', 'testpass')
        
        inputs = iter(["save_key", "save_secret", "y"])
        monkeypatch.setattr('builtins.input', lambda _: next(inputs))
        monkeypatch.setattr('getpass.getpass', lambda _: next(inputs))
        
        get_or_prompt_secrets(secrets_file)
        
        assert secrets_file.exists()
        # Verify it's encrypted (has version key)
        import json
        with open(secrets_file) as f:
            data = json.load(f)
        assert data.get('version') == 1
        assert 'salt' in data
        assert 'data' in data


class TestEncryption:
    """Tests for encryption round-trip."""
    
    def test_encrypt_decrypt_round_trip(self):
        from onshape_export_tool import encrypt_secrets, decrypt_secrets, Secrets
        
        original = Secrets(access_key="test_access", secret_key="test_secret")
        password = "mypassword"
        
        encrypted = encrypt_secrets(original, password)
        decrypted = decrypt_secrets(encrypted, password)
        
        assert decrypted['access_key'] == original['access_key']
        assert decrypted['secret_key'] == original['secret_key']
    
    def test_wrong_password_fails(self):
        from onshape_export_tool import encrypt_secrets, decrypt_secrets, Secrets
        from cryptography.fernet import InvalidToken
        
        original = Secrets(access_key="test", secret_key="secret")
        encrypted = encrypt_secrets(original, "correctpass")
        
        with pytest.raises(InvalidToken):
            decrypt_secrets(encrypted, "wrongpass")
