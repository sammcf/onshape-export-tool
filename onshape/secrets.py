"""Secrets and configuration management.

Handles API credentials encryption, storage, and document configuration.
"""
import json
import logging
from pathlib import Path
from typing import Optional
from typing_extensions import TypedDict


class Secrets(TypedDict):
    access_key: str
    secret_key: str


_cached_password: Optional[str] = None


def derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2 key derivation, 480k iterations per OWASP."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    import base64
    
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt_secrets(secrets: Secrets, password: str) -> dict:
    from cryptography.fernet import Fernet
    import os
    import base64
    
    salt = os.urandom(16)
    key = derive_key(password, salt)
    fernet = Fernet(key)
    
    plaintext = json.dumps({
        'accessKey': secrets['access_key'],
        'secretKey': secrets['secret_key']
    }).encode()
    
    encrypted = fernet.encrypt(plaintext)
    
    return {
        'version': 1,
        'salt': base64.b64encode(salt).decode(),
        'data': encrypted.decode()
    }


def decrypt_secrets(storage: dict, password: str) -> Secrets:
    from cryptography.fernet import Fernet
    import base64
    
    salt = base64.b64decode(storage['salt'])
    key = derive_key(password, salt)
    fernet = Fernet(key)
    
    decrypted = fernet.decrypt(storage['data'].encode())
    data = json.loads(decrypted.decode())
    
    return Secrets(
        access_key=data.get('accessKey') or data.get('access_key'),
        secret_key=data.get('secretKey') or data.get('secret_key')
    )


def prompt_password(confirm: bool = False) -> str:
    import getpass
    
    while True:
        password = getpass.getpass("  Encryption password: ")
        if not password:
            print("  Password cannot be empty.")
            continue
        
        if confirm:
            password2 = getpass.getpass("  Confirm password: ")
            if password != password2:
                print("  Passwords do not match. Try again.")
                continue
        
        return password


def get_password(confirm: bool = False) -> str:
    global _cached_password
    if _cached_password is None:
        _cached_password = prompt_password(confirm=confirm)
    return _cached_password


def load_secrets(path: Path) -> Optional[Secrets]:
    """Handles both encrypted (v1) and plaintext (v0) formats."""
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        
        # Check for encrypted format (v1)
        if data.get('version') == 1:
            password = get_password()
            try:
                return decrypt_secrets(data, password)
            except Exception as e:
                logging.error(f"Failed to decrypt secrets: {e}")
                return None
        
        # Plaintext format (v0) - will be auto-migrated on next save
        access_key = data.get('accessKey') or data.get('access_key')
        secret_key = data.get('secretKey') or data.get('secret_key')
        if access_key and secret_key:
            return Secrets(access_key=access_key, secret_key=secret_key)
        return None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_secrets(secrets: Secrets, path: Path) -> None:
    password = get_password(confirm=True)
    encrypted = encrypt_secrets(secrets, password)
    
    with open(path, 'w') as f:
        json.dump(encrypted, f, indent=2)
    logging.info(f"Saved encrypted secrets to {path}")
    print(f"  Note: To change your encryption password, delete {path} and re-run setup.")


def prompt_secrets() -> Secrets:
    import getpass
    print("\n--- Onshape API Credentials ---")
    print("Enter your Onshape API keys (from Developer Portal):\n")
    
    while True:
        try:
            access_key = input("  Access Key: ").strip()
            break
        except UnicodeDecodeError:
            print("  Error: Invalid characters. Please try again.")
    
    while True:
        try:
            secret_key = getpass.getpass("  Secret Key: ").strip()
            break
        except UnicodeDecodeError:
            print("  Error: Invalid characters. Please try again.")
    
    return Secrets(access_key=access_key, secret_key=secret_key)


def get_or_prompt_secrets(path: Path) -> Secrets:
    secrets = load_secrets(path)
    if secrets:
        return secrets
    
    print(f"No valid secrets found at {path}")
    secrets = prompt_secrets()
    
    # Offer to save
    save_choice = input("\nSave these credentials for future use? [y/N]: ").strip().lower()
    if save_choice == 'y':
        save_secrets(secrets, path)
        print(f"Saved to {path}")
    
    return secrets


# --- Document Configuration ---

def prompt_document_config() -> tuple[str, str]:
    print("\n--- Document Configuration ---\n")
    print("You can find these IDs in the Onshape document URL:")
    print("  https://cad.onshape.com/documents/{documentId}/w/{workspaceId}/...\n")
    
    did = input("  Document ID: ").strip()
    wid = input("  Workspace ID: ").strip()
    return did, wid


def save_document_config(did: str, wid: str, path: Path) -> None:
    data = {
        'documentId': did,
        'workspaceId': wid
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Saved document config to {path}")


def load_document_config(path: Path) -> Optional[tuple[str, str]]:
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        did = data.get('documentId')
        wid = data.get('workspaceId')
        if did and wid and did != "YOUR_DOCUMENT_ID_HERE" and wid != "YOUR_WORKSPACE_ID_HERE":
            return did, wid
        return None
    except (FileNotFoundError, json.JSONDecodeError):
        return None
