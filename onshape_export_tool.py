#!/usr/bin/env python3
"""Onshape Manufacturing Export Tool

Automates DXF/PDF exports from Onshape documents via REST API.
"""
import json
import sys
import logging
import argparse
import requests
import time
import zipfile
import re
from pathlib import Path
from functools import reduce
from typing import Dict, Any, List, Optional, Tuple, Callable, TypeVar, cast
from typing_extensions import TypedDict


# ============================================================
# SECTION 1: Configuration & Constants
# ============================================================

API_BASE = "https://cad.onshape.com/api/v12"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

# Default drawing template (Onshape's ANSI A template, used with ISO settings)
DEFAULT_TEMPLATE_DOC = "09fb14dcb55eee217f55fa7b"
DEFAULT_TEMPLATE_ELEMENT = "149ce62208ba05ac0cee75e5"

# Prefixes for temporary elements that should be cleaned up
TEMP_ELEMENT_PREFIXES = ("TEMP_", "DEBUG_VIEW_", "TEST_MV_")

# Onshape property IDs for metadata lookup
PROP_PART_NUMBER = "57f3fb8efa3416c06701d60f"
PROP_REVISION = "57f3fb8efa3416c06701d610"
PROP_MATERIAL = "57f3fb8efa3416c06701d615"

# Type alias for export results: (element_id, filename)
ExportResult = Tuple[str, str]

# Type alias for translation results: (element_id, export_rule_filename or None)
TranslationResult = Tuple[str, Optional[str]]


def get_run_command() -> str:
    """Returns appropriate CLI command for frozen exe vs script mode."""
    if getattr(sys, 'frozen', False):
        return "./onshape_export_tool"
    return "python onshape_export_tool.py"


# ============================================================
# SECTION 1b: Document Context
# ============================================================

class DocContext(TypedDict):
    """Onshape API paths use /d/{did}/{wvm_type}/{wvm_id}/... format.
    This bundles those values, enabling workspace/version mode switching.
    """
    did: str
    wvm_type: str  # 'w' = workspace (mutable), 'v' = version, 'm' = microversion
    wvm_id: str


def doc_path(ctx: DocContext, suffix: str = "") -> str:
    """Build document path segment: /d/{did}/{wvm_type}/{wvm_id}{suffix}"""
    return f"/d/{ctx['did']}/{ctx['wvm_type']}/{ctx['wvm_id']}{suffix}"


def is_mutable(ctx: DocContext) -> bool:
    """Check if context allows modifications (workspace only)."""
    return ctx['wvm_type'] == 'w'


def make_context(did: str, wid: str) -> DocContext:
    """Create a workspace context (convenience for migration)."""
    return DocContext(did=did, wvm_type='w', wvm_id=wid)


def make_version_context(did: str, vid: str) -> DocContext:
    """Create a version context for read-only exports."""
    return DocContext(did=did, wvm_type='v', wvm_id=vid)


# ============================================================
# SECTION 1c: Secrets Management
# ============================================================

class Secrets(TypedDict):
    """API credentials for Onshape authentication."""
    access_key: str
    secret_key: str


# Password cache for session (avoid repeated prompts)
_cached_password: Optional[str] = None


def derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2 key derivation. 480k iterations per OWASP recommendation."""
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
    """Encrypt secrets, returning versioned storage dict."""
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
    """Decrypt secrets from versioned storage dict."""
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
    """Prompt for encryption password. With confirm=True, requires double entry."""
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
    """Get password from cache or prompt user."""
    global _cached_password
    if _cached_password is None:
        _cached_password = prompt_password(confirm=confirm)
    return _cached_password


def load_secrets(path: Path) -> Optional[Secrets]:
    """Load secrets, handling both encrypted (v1) and plaintext (v0) formats."""
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
    """Save secrets encrypted. Prompts for password if not cached."""
    password = get_password(confirm=True)
    encrypted = encrypt_secrets(secrets, password)
    
    with open(path, 'w') as f:
        json.dump(encrypted, f, indent=2)
    logging.info(f"Saved encrypted secrets to {path}")
    print(f"  Note: To change your encryption password, delete {path} and re-run setup.")


def prompt_secrets() -> Secrets:
    """Prompt for API credentials. Retries on clipboard encoding issues."""
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
    """Load or prompt, offering to persist for future runs."""
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


# ============================================================
# SECTION 1d: Interactive Utilities
# ============================================================

def interactive_select(items: List[Dict[str, Any]], prompt: str, 
                       display_fn: Callable[[Dict[str, Any]], str]) -> Optional[Dict[str, Any]]:
    """Numbered menu. Returns None on cancel (0) or empty list."""
    if not items:
        print("No items available.")
        return None
    
    print(f"\n{prompt}\n")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {display_fn(item)}")
    print("  0. Cancel\n")
    
    while True:
        try:
            choice = input("Enter number: ").strip()
            if not choice:
                continue
            idx = int(choice)
            if idx == 0:
                return None
            if 1 <= idx <= len(items):
                return items[idx - 1]
            print(f"Please enter 1-{len(items)} or 0 to cancel")
        except ValueError:
            print("Please enter a number")


def prompt_document_config() -> Tuple[str, str]:
    """Prompt user for document ID and workspace ID."""
    print("\n--- Document Configuration ---\n")
    print("You can find these IDs in the Onshape document URL:")
    print("  https://cad.onshape.com/documents/{documentId}/w/{workspaceId}/...\n")
    
    did = input("  Document ID: ").strip()
    wid = input("  Workspace ID: ").strip()
    return did, wid


def save_document_config(did: str, wid: str, path: Path) -> None:
    """Save document configuration to file."""
    data = {
        'documentId': did,
        'workspaceId': wid
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Saved document config to {path}")


def load_document_config(path: Path) -> Optional[Tuple[str, str]]:
    """Load document config from file. Returns (did, wid) or None."""
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


def run_setup_wizard(secrets_path: Path, config_path: Path) -> None:
    """Run interactive setup wizard for first-time configuration."""
    print("\n" + "="*60)
    print("    ONSHAPE EXPORT TOOL - SETUP WIZARD")
    print("="*60)
    
    # Step 1: API Credentials (only if not already configured)
    print("\nStep 1: API Credentials")
    print("-" * 40)
    existing_secrets = load_secrets(secrets_path)
    if existing_secrets:
        print(f"✓ Secrets already configured ({secrets_path})")
    else:
        secrets = prompt_secrets()
        save_secrets(secrets, secrets_path)
    
    # Step 2: Document Configuration
    print("\nStep 2: Document Configuration")
    print("-" * 40)
    did, wid = prompt_document_config()
    save_document_config(did, wid, config_path)
    
    print("\n" + "="*60)
    print("    SETUP COMPLETE!")
    print("="*60)
    print(f"\nSecrets: {secrets_path}")
    print(f"Config: {config_path}")
    print(f"\nYou can now run exports with: {get_run_command()}")


# ============================================================
# SECTION 2: Custom Exceptions
# ============================================================

class TranslationError(Exception):
    """Raised when a translation job fails."""
    pass

class ElementNotFoundError(Exception):
    """Raised when an expected element disappears."""
    pass


# ============================================================
# SECTION 3: Generic Utilities
# ============================================================

T = TypeVar('T')


# ============================================================
# SECTION 3a: Pipeline Composition Utilities
# ============================================================

def pipeline(*steps: Callable[[T], T]) -> Callable[[T], T]:
    """Compose functions left-to-right: pipeline(f, g, h)(x) = h(g(f(x)))
    
    Each step receives the output of the previous step.
    This enables functional workflow composition.
    """
    return lambda initial: reduce(lambda state, step: step(state), steps, initial)


class WorkflowState(TypedDict, total=False):
    """Immutable state passed between workflow steps.
    
    Each step returns a new state dict: {**state, 'key': new_value}
    Using total=False allows optional keys.
    """
    # Bound dependencies (injected at start)
    client: Any  # OnshapeClient
    ctx: DocContext
    output_dir: Path
    # Workflow data (accumulated by steps)
    results: List[ExportResult]
    log_entries: List[str]
    part_studios: List[Dict[str, Any]]
    drawings: List[Dict[str, Any]]
    # Final output
    zip_path: Optional[Path]
    collision_warnings: List[str]


def log_step(state: WorkflowState, msg: str) -> WorkflowState:
    """Helper to add a log entry to state (pure function)."""
    entry = f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}"
    logging.info(msg)
    log_entries = state.get('log_entries', []).copy()
    log_entries.append(entry)
    return {**state, 'log_entries': log_entries}


def poll_until(
    fetch: Callable[[], Any],
    predicate: Callable[[Any], Optional[T]],
    timeout: int = 60,
    interval: float = 2.0
) -> T:
    """Poll until predicate returns non-None. Used for translation status checks."""
    start = time.time()
    while time.time() - start < timeout:
        data = fetch()
        result = predicate(data)
        if result is not None:
            return result
        time.sleep(interval)
    raise TimeoutError(f"Polling timed out after {timeout}s")


# ============================================================
# SECTION 4: OnshapeClient (Transport Only)
# ============================================================

class OnshapeClient:
    """HTTP transport only. Business logic is in standalone functions that accept client.
    
    Uses HTTP Basic auth with Onshape API keys (not OAuth).
    Returns parsed JSON or raw bytes depending on Content-Type.
    """
    
    def __init__(self, access_key: str, secret_key: str, base_url: str = API_BASE):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.auth = (access_key, secret_key)
        self.session.headers.update({
            'Accept': 'application/vnd.onshape.v1+json',
            'Content-Type': 'application/json'
        })

    def request(self, method: str, endpoint: str, **kwargs) -> Any:
        """404 on /translations often means missing export rule in Onshape."""
        url = endpoint if endpoint.startswith('http') else f"{self.base_url}{endpoint}"
        try:
            logging.debug(f"API Request: {method} {url}")
            response = self.session.request(method, url, **kwargs)
            if response.status_code >= 400:
                logging.error(f"Error {response.status_code}: {response.text}")
                # Provide helpful hint for 404 errors on translation endpoints
                if response.status_code == 404 and '/translations' in endpoint:
                    logging.error(
                        "HINT: A 404 on translation endpoints often indicates a missing export rule. "
                        "Check that you have a valid export rule configured in Onshape for this "
                        "element type (Part Studio DXF, Drawing PDF, etc.)."
                    )
            response.raise_for_status()
            
            content_type = response.headers.get('Content-Type', '')
            if 'application/json' in content_type:
                return response.json()
            return response.content
        except requests.RequestException as e:
            logging.error(f"API request failed: {e}")
            raise


# ============================================================
# SECTION 5: API Operations (Standalone Functions)
# ============================================================

def list_elements(client: OnshapeClient, ctx: DocContext) -> List[Dict[str, Any]]:
    """List all elements (tabs) in a document."""
    endpoint = f"/documents{doc_path(ctx)}/elements"
    resp = client.request('GET', endpoint)
    return resp if isinstance(resp, list) else resp.get('elements', [])


def get_features(client: OnshapeClient, ctx: DocContext, eid: str) -> List[Dict[str, Any]]:
    """Get all features from a Part Studio."""
    endpoint = f"/partstudios{doc_path(ctx)}/e/{eid}/features"
    resp = client.request('GET', endpoint)
    return resp.get('features', [])


def list_parts(
    client: OnshapeClient, ctx: DocContext, eid: str,
    include_flat_parts: bool = False
) -> List[Dict[str, Any]]:
    """List all parts in a Part Studio."""
    endpoint = f"/parts{doc_path(ctx)}/e/{eid}"
    params = {}
    if include_flat_parts:
        params['includeFlatParts'] = 'true'
    return client.request('GET', endpoint, params=params)


def categorize_parts(parts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Separate flat patterns from regular parts.
    
    Sheet metal flat patterns export directly; their parent parts are filtered out.
    """
    flat_patterns = []
    regular_parts = []
    flat_part_originals = set()  # IDs of parts that have flat patterns
    
    for part in parts:
        if part.get('isFlattenedBody'):
            flat_patterns.append(part)
            if part.get('unflattenedPartId'):
                flat_part_originals.add(part['unflattenedPartId'])
        else:
            regular_parts.append(part)
    
    # Filter out regular parts that are original sheet metal (they have flat patterns)
    regular_parts = [p for p in regular_parts if p.get('partId') not in flat_part_originals]
    
    return flat_patterns, regular_parts


def get_part_metadata(
    client: OnshapeClient, ctx: DocContext, eid: str, part_id: str,
    include_computed: bool = True
) -> Dict[str, Any]:
    """Get metadata for a specific part, optionally including computed properties."""
    endpoint = f"/metadata{doc_path(ctx)}/e/{eid}/p/{part_id}"
    params = {}
    if include_computed:
        params['includeComputedProperties'] = 'true'
    return client.request('GET', endpoint, params=params)


def get_part_bounding_box(
    client: OnshapeClient, ctx: DocContext, eid: str, part_id: str
) -> Dict[str, float]:
    """Get bounding box for a specific part. Returns dict with lowX/Y/Z, highX/Y/Z."""
    endpoint = f"/parts{doc_path(ctx)}/e/{eid}/partid/{part_id}/boundingboxes"
    return client.request('GET', endpoint)


def get_part_thickness(
    client: OnshapeClient, ctx: DocContext, eid: str, part_id: str,
    property_name: str = "Thickness"
) -> Optional[float]:
    """Returns mm. Tries computed property first, falls back to bounding box Z-height."""
    # Approach 1: Try to read computed property
    try:
        metadata = get_part_metadata(client, ctx, eid, part_id, include_computed=True)
        properties = metadata.get('properties', [])
        
        for prop in properties:
            if prop.get('name') == property_name or prop.get('propertyId', '').endswith(property_name):
                value = prop.get('value')
                if isinstance(value, dict):
                    # Value with units: {"value": 3.0, "unitString": "mm"}
                    raw_value = value.get('value', 0)
                    unit = value.get('unitString', 'mm')
                    # Convert to mm if needed
                    if unit == 'm':
                        return raw_value * 1000
                    elif unit == 'in':
                        return raw_value * 25.4
                    return raw_value
                elif isinstance(value, (int, float)):
                    return float(value)
        
        logging.debug(f"Computed property '{property_name}' not found for part {part_id}")
    except Exception as e:
        logging.debug(f"Failed to get metadata for part {part_id}: {e}")
    
    # Approach 2: Fall back to bounding box Z-height
    try:
        bbox = get_part_bounding_box(client, ctx, eid, part_id)
        # For oriented flat parts, Z-height is thickness
        # Bounding box values are in meters
        z_height = abs(bbox.get('highZ', 0) - bbox.get('lowZ', 0))
        thickness_mm = z_height * 1000  # Convert m to mm
        
        if thickness_mm > 0.01:  # Ignore near-zero values
            logging.debug(f"Using bounding box Z-height for thickness: {thickness_mm:.2f}mm")
            return thickness_mm
    except Exception as e:
        logging.debug(f"Failed to get bounding box for part {part_id}: {e}")
    
    return None


def format_thickness_prefix(thickness_mm: Optional[float]) -> str:
    """Returns e.g. '3mm' for filenames. Empty string if None/invalid."""
    if thickness_mm is None or thickness_mm <= 0:
        return ""
    
    # Format to 1 decimal place, removing trailing zeros
    formatted = f"{thickness_mm:.1f}".rstrip('0').rstrip('.')
    return f"{formatted}mm"


class PartProperties(TypedDict, total=False):
    """Properties fetched from Onshape metadata for filename assembly."""
    part_number: str
    revision: str
    material: str


def get_part_properties(
    client: OnshapeClient, ctx: DocContext, eid: str, part_id: str
) -> Tuple[PartProperties, List[str]]:
    """Fetch part properties for filename assembly.
    
    Returns:
        Tuple of (properties dict, list of missing property names)
    """
    props: PartProperties = {}
    missing: List[str] = []
    
    try:
        metadata = get_part_metadata(client, ctx, eid, part_id, include_computed=False)
        properties = metadata.get('properties', [])
        
        # Build lookup by propertyId
        prop_lookup = {p.get('propertyId'): p.get('value', '') for p in properties}
        
        # Extract required properties
        if PROP_PART_NUMBER in prop_lookup and prop_lookup[PROP_PART_NUMBER]:
            props['part_number'] = str(prop_lookup[PROP_PART_NUMBER])
        else:
            missing.append('Part Number')
        
        if PROP_REVISION in prop_lookup and prop_lookup[PROP_REVISION]:
            props['revision'] = str(prop_lookup[PROP_REVISION])
        else:
            missing.append('Revision')
        
        # Material is a nested dict with displayName
        material_val = prop_lookup.get(PROP_MATERIAL)
        if material_val:
            if isinstance(material_val, dict):
                props['material'] = material_val.get('displayName', '')
            else:
                props['material'] = str(material_val)
            if not props.get('material'):
                missing.append('Material')
        else:
            missing.append('Material')
            
    except Exception as e:
        logging.warning(f"Failed to get properties for part {part_id}: {e}")
        missing = ['Part Number', 'Revision', 'Material']
    
    return props, missing


def build_dxf_filename(
    part_name: str,
    thickness_mm: Optional[float],
    props: PartProperties
) -> str:
    """Build DXF filename: {thickness}mm {material}_{partNumber}_Rev {revision}.dxf"""
    thickness_str = format_thickness_prefix(thickness_mm)
    
    part_number = props.get('part_number', '')
    revision = props.get('revision', '')
    material = props.get('material', '')
    
    # If we have all properties, use the full schema
    if part_number and revision and material:
        return f"{thickness_str} {material}_{part_number}_Rev {revision}.dxf"
    
    # Fallback: use part name with thickness prefix
    return f"{thickness_str}{part_name}.dxf" if thickness_str else f"{part_name}.dxf"


def build_pdf_filename(name: str, props: PartProperties) -> str:
    """Build PDF filename: {partNumber}_Rev {revision}.pdf"""
    part_number = props.get('part_number', '')
    revision = props.get('revision', '')
    
    # If we have required properties, use schema
    if part_number and revision:
        return f"{part_number}_Rev {revision}.pdf"
    
    # Fallback: use provided name
    return f"{name}.pdf" if not name.lower().endswith('.pdf') else name


def update_feature_suppression(
    client: OnshapeClient, 
    ctx: DocContext,
    eid: str, 
    feature: Dict[str, Any], 
    suppressed: bool
) -> None:
    """Suppress or unsuppress a feature in a Part Studio."""
    feature_id = feature.get('featureId')
    feature_copy = json.loads(json.dumps(feature))
    feature_copy['suppressed'] = suppressed
    
    payload = {
        "feature": feature_copy,
        "serializationVersion": "1.2.15",
        "sourceMicroversion": ""
    }
    endpoint = f"/partstudios{doc_path(ctx)}/e/{eid}/features/featureid/{feature_id}"
    client.request('POST', endpoint, json=payload)


def delete_element(client: OnshapeClient, ctx: DocContext, eid: str) -> None:
    """Delete an element from the document."""
    endpoint = f"/elements{doc_path(ctx)}/e/{eid}"
    logging.info(f"Deleting element {eid}")
    client.request('DELETE', endpoint)


def rename_element(client: OnshapeClient, ctx: DocContext, eid: str, new_name: str) -> None:
    """Rename an element (tab) in the document to match the assembled filename."""
    # The 'Name' property has a standard propertyId
    endpoint = f"/metadata{doc_path(ctx)}/e/{eid}"
    payload = {
        "properties": [
            {"propertyId": "57f3fb8efa3416c06701d61d", "value": new_name}
        ]
    }
    logging.debug(f"Renaming element {eid} to '{new_name}'")
    try:
        client.request('POST', endpoint, json=payload)
    except Exception as e:
        logging.warning(f"Failed to rename element {eid}: {e}")


def get_drawing_references(
    client: OnshapeClient, ctx: DocContext, drawing_eid: str
) -> List[Dict[str, Any]]:
    """Get parts/assemblies referenced by a drawing.
    
    Returns list of reference dicts with elementId, partId, etc.
    """
    endpoint = f"/appelements{doc_path(ctx)}/e/{drawing_eid}/references"
    try:
        resp = client.request('GET', endpoint)
        logging.debug(f"Drawing references response: {resp}")
        if isinstance(resp, list):
            return resp
        elif isinstance(resp, dict):
            # Try different possible response structures
            return resp.get('referencedElements', resp.get('references', []))
        return []
    except Exception as e:
        logging.debug(f"Failed to get drawing references: {e}")
        return []


def create_drawing(client: OnshapeClient, ctx: DocContext, name: str) -> str:
    """Create an empty drawing. Returns the new element ID."""
    endpoint = f"/drawings{doc_path(ctx)}/create"
    payload = {
        "drawingName": name,
        "standard": "ISO",
        "templateDocumentId": DEFAULT_TEMPLATE_DOC,
        "templateElementId": DEFAULT_TEMPLATE_ELEMENT,
        "units": "MILLIMETER",
        "size": "A",
        "border": False,
        "titleblock": False
    }
    logging.info(f"Creating drawing '{name}'")
    resp = client.request('POST', endpoint, json=payload)
    return cast(str, resp.get('id'))


def add_view_to_drawing(
    client: OnshapeClient, 
    ctx: DocContext,
    drawing_eid: str,
    source_eid: str, 
    part_id: str
) -> None:
    """Add a 1:1 top view of a part to a drawing."""
    endpoint = f"/drawings{doc_path(ctx)}/e/{drawing_eid}/modify"
    payload = {
        "description": "Add Top View 1:1",
        "jsonRequests": [
            {
                "messageName": "onshapeCreateViews",
                "formatVersion": "2021-01-01",
                "description": "Add top view",
                "views": [
                    {
                        "viewType": "TopLevel",
                        "position": {"x": 0.1, "y": 0.1},
                        "scale": {"scaleSource": "Custom", "numerator": 1, "denumerator": 1},
                        "orientation": "top",
                        "showCentermarks": False,
                        "showCenterlines": False,
                        "reference": {
                            "elementId": source_eid,
                            "idTag": part_id
                        }
                    }
                ]
            }
        ]
    }
    logging.info(f"Adding view of part {part_id} to drawing {drawing_eid}")
    client.request('POST', endpoint, json=payload)


def initiate_translation(
    client: OnshapeClient, 
    ctx: DocContext,
    eid: str, 
    format_name: str, 
    destination_name: str
) -> str:
    """Start a translation (export) job. Returns the translation ID."""
    endpoint = f"/drawings{doc_path(ctx)}/e/{eid}/translations"
    payload = {
        "formatName": format_name,
        "storeInDocument": True,
        "evaluateExportRule": True,
        "destinationName": destination_name,
        "includeFormedCentermarks": False
    }
    
    # Add DXF-specific options for flat pattern exports
    if format_name == 'DXF':
        payload["includeBendCenterlines"] = True
        payload["includeBendLines"] = False
    
    logging.info(f"Initiating {format_name} translation for element {eid}")
    resp = client.request('POST', endpoint, json=payload)
    return cast(str, resp.get('id'))


def poll_translation(client: OnshapeClient, translation_id: str, timeout: int = 300) -> TranslationResult:
    """Poll until translation completes. Returns (result_element_id, export_rule_filename)."""
    endpoint = f"/translations/{translation_id}"
    
    def fetch():
        return client.request('GET', endpoint)
    
    def check_state(resp):
        state = resp.get('requestState')
        if state == 'DONE':
            ids = resp.get('resultElementIds', [])
            if ids:
                export_rule_filename = resp.get('exportRuleFileName')  # May be None
                return (ids[0], export_rule_filename)
            raise TranslationError("Translation done but no result element IDs found")
        elif state == 'FAILED':
            raise TranslationError(f"Translation failed: {resp.get('failureReason', 'Unknown reason')}")
        return None  # Keep polling
    
    return poll_until(fetch, check_state, timeout)


def wait_for_microversion_change(
    client: OnshapeClient, 
    ctx: DocContext,
    eid: str, 
    old_mv: Optional[str], 
    timeout: int = 60
) -> str:
    """Poll until an element's microversion changes. Returns the new microversion."""
    logging.info(f"Waiting for element {eid} to update...")
    
    def fetch():
        elements = list_elements(client, ctx)
        return next((e for e in elements if e['id'] == eid), None)
    
    def check_microversion(element):
        if element is None:
            raise ElementNotFoundError(f"Element {eid} disappeared!")
        new_mv = element.get('microversionId')
        if new_mv and new_mv != old_mv:
            logging.info(f"Element updated (MV: {new_mv})")
            return new_mv
        return None  # Keep polling
    
    result = poll_until(fetch, check_microversion, timeout)
    # Small buffer for drawing app to finish internal rendering
    time.sleep(2)
    return result


def download_blob(client: OnshapeClient, ctx: DocContext, eid: str) -> bytes:
    """Download blob element content as bytes."""
    endpoint = f"/blobelements{doc_path(ctx)}/e/{eid}"
    logging.debug(f"Downloading blob {eid}")
    return cast(bytes, client.request('GET', endpoint))


def get_element_microversion(client: OnshapeClient, ctx: DocContext, eid: str) -> Optional[str]:
    """Get the current microversion ID of an element."""
    elements = list_elements(client, ctx)
    element = next((e for e in elements if e['id'] == eid), None)
    return element.get('microversionId') if element else None


# ============================================================
# SECTION 5a: Interactive API Functions
# ============================================================

def list_documents(client: OnshapeClient, limit: int = 20) -> List[Dict[str, Any]]:
    """List recently modified documents.
    
    Returns list of documents with id, name, modifiedAt, etc.
    """
    response = client.request('GET', '/documents', params={
        'sortColumn': 'modifiedAt',
        'sortOrder': 'desc',
        'limit': limit
    })
    return response.get('items', []) if isinstance(response, dict) else response


def list_workspaces(client: OnshapeClient, did: str) -> List[Dict[str, Any]]:
    """List workspaces in a document."""
    return client.request('GET', f'/documents/d/{did}/workspaces')


def list_versions(client: OnshapeClient, did: str) -> List[Dict[str, Any]]:
    """List versions in a document."""
    return client.request('GET', f'/documents/d/{did}/versions')


def run_interactive_workflow(client: OnshapeClient, output_dir: Path,
                             clean_before: bool = False, clean_after: bool = False) -> Optional[Path]:
    """Run export workflow with interactive document/element selection.
    
    Flow:
    1. List and select document
    2. Choose workspace or version
    3. Run export on selected context
    """
    print("\n" + "="*60)
    print("    INTERACTIVE EXPORT")
    print("="*60)
    
    # Step 1: Select document
    print("\nFetching recent documents...")
    documents = list_documents(client)
    if not documents:
        print("No documents found.")
        return None
    
    doc = interactive_select(
        documents,
        "Select a document:",
        lambda d: f"{d['name']} (modified: {d.get('modifiedAt', 'unknown')[:10]})"
    )
    if not doc:
        print("Cancelled.")
        return None
    
    did = doc['id']
    print(f"\nSelected: {doc['name']}")
    
    # Step 2: Choose workspace or version
    print("\nFetching workspaces and versions...")
    workspaces = list_workspaces(client, did)
    versions = list_versions(client, did)
    
    # Build combined list
    options = []
    for ws in workspaces:
        options.append({'type': 'workspace', 'id': ws['id'], 'name': ws.get('name', 'Main'), 'data': ws})
    for v in versions:
        options.append({'type': 'version', 'id': v['id'], 'name': v.get('name', 'Unnamed'), 'data': v})
    
    if not options:
        print("No workspaces or versions found.")
        return None
    
    choice = interactive_select(
        options,
        "Select workspace or version:",
        lambda o: f"[{o['type'].upper()}] {o['name']}"
    )
    if not choice:
        print("Cancelled.")
        return None
    
    # Step 3: Create context and run
    if choice['type'] == 'workspace':
        ctx = make_context(did, choice['id'])
        print(f"\nExporting from workspace: {choice['name']}")
    else:
        ctx = make_version_context(did, choice['id'])
        print(f"\nExporting from version: {choice['name']}")
        if clean_before or clean_after:
            print("Note: --clean flags ignored for version exports")
            clean_before = clean_after = False
    
    return run_export_workflow(client, ctx, output_dir, 
                               clean_before=clean_before, clean_after=clean_after)


# ============================================================
# SECTION 6: Business Logic (Workflow Functions)
# ============================================================

# Each function has a single responsibility and can be composed
# to create different export workflows.

def cleanup_temp_elements(client: OnshapeClient, ctx: DocContext) -> int:
    """Delete any leftover temporary elements from previous runs.
    
    Returns:
        Number of elements deleted
    """
    elements = list_elements(client, ctx)
    temp_elements = [e for e in elements if e.get('name', '').startswith(TEMP_ELEMENT_PREFIXES)]
    
    deleted = 0
    for e in temp_elements:
        try:
            delete_element(client, ctx, e['id'])
            deleted += 1
        except Exception as ex:
            logging.debug(f"Failed to delete leftover {e['id']}: {ex}")
    
    if deleted > 0:
        logging.info(f"Cleaned up {deleted} temporary elements")
    return deleted


def find_blobs_by_extension(
    client: OnshapeClient, 
    ctx: DocContext, 
    extensions: Tuple[str, ...]
) -> List[Dict[str, Any]]:
    """Find blob elements matching given extensions (e.g., '.dxf', '.pdf').
    
    Args:
        extensions: Tuple of lowercase extensions including the dot
        
    Returns:
        List of matching blob element dicts
    """
    elements = list_elements(client, ctx)
    blobs = []
    for e in elements:
        if e.get('elementType') == 'BLOB':
            name = e.get('name', '').lower()
            if any(name.endswith(ext) for ext in extensions):
                blobs.append(e)
    return blobs


def delete_elements(
    client: OnshapeClient, 
    ctx: DocContext, 
    elements: List[Dict[str, Any]]
) -> int:
    """Delete multiple elements. Returns count successfully deleted."""
    deleted = 0
    for e in elements:
        try:
            delete_element(client, ctx, e['id'])
            deleted += 1
        except Exception as ex:
            logging.debug(f"Failed to delete {e['id']}: {ex}")
    return deleted


def cleanup_exports(client: OnshapeClient, ctx: DocContext) -> int:
    """Delete all DXF and PDF blobs from document.
    
    Composable cleanup operation for --clean flag.
    Only works on mutable contexts (workspaces).
    
    Returns:
        Number of elements deleted
    """
    if not is_mutable(ctx):
        logging.warning("Cannot cleanup exports in immutable context (version/microversion)")
        return 0
    
    blobs = find_blobs_by_extension(client, ctx, ('.dxf', '.pdf'))
    if not blobs:
        logging.info("No DXF/PDF blobs to clean up")
        return 0
    
    logging.info(f"Cleaning up {len(blobs)} DXF/PDF files...")
    deleted = delete_elements(client, ctx, blobs)
    logging.info(f"Deleted {deleted} export files")
    return deleted


def discover_exportables(
    client: OnshapeClient, 
    ctx: DocContext
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Categorize document elements into Part Studios and Drawings.
    
    Returns:
        Tuple of (part_studios, drawings) where each is a list of element dicts
    """
    elements = list_elements(client, ctx)
    
    part_studios = [e for e in elements if e['elementType'] == 'PARTSTUDIO']
    drawings = [
        e for e in elements 
        if e['elementType'] == 'DRAWING' or 
        (e['elementType'] == 'APPLICATION' and 'drawing' in e.get('dataType', '').lower())
    ]
    
    # Filter out temp drawings
    drawings = [d for d in drawings if not d['name'].startswith(TEMP_ELEMENT_PREFIXES)]
    
    logging.info(f"Discovered {len(part_studios)} Part Studios and {len(drawings)} drawings")
    return part_studios, drawings


def find_orient_feature(features: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the highest-indexed 'Orient Plates for Export' feature.
    
    Returns:
        The feature dict, or None if not found
    """
    pattern = re.compile(r"^Orient Plates for Export(?: (\d+))?$")
    candidates = []
    
    for f in features:
        match = pattern.match(f.get('name', ''))
        if match:
            index = int(match.group(1)) if match.group(1) else 0
            candidates.append((index, f))
    
    if not candidates:
        return None
    
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def export_part_as_dxf(
    client: OnshapeClient,
    ctx: DocContext,
    part_studio_eid: str,
    part: Dict[str, Any]
) -> Optional[ExportResult]:
    """Export a single part as DXF via temporary drawing.
    
    Creates a temp drawing, adds a top view, exports to DXF, then cleans up.
    Prepends part thickness to filename if available.
    
    Returns:
        (result_element_id, filename) tuple on success, None on failure
    """
    part_id = cast(str, part.get('partId'))
    part_name = cast(str, part.get('name', 'unnamed_part'))
    
    # Create temp drawing
    temp_name = f"TEMP_{part_name}_{int(time.time())}"
    temp_drawing_id = create_drawing(client, ctx, temp_name)
    old_mv = get_element_microversion(client, ctx, temp_drawing_id)
    logging.info(f"Created temp drawing for '{part_name}'")
    
    try:
        # Add view and wait for it to render
        add_view_to_drawing(client, ctx, temp_drawing_id, part_studio_eid, part_id)
        wait_for_microversion_change(client, ctx, temp_drawing_id, old_mv)
        
        # Get part thickness from bounding box Z-height
        thickness = get_part_thickness(client, ctx, part_studio_eid, part_id)
        if thickness:
            logging.debug(f"Part '{part_name}' thickness: {thickness:.2f}mm")
        
        # Get part properties for filename
        props, missing = get_part_properties(client, ctx, part_studio_eid, part_id)
        if missing:
            logging.warning(f"Part '{part_name}' missing properties: {', '.join(missing)}")
        
        # Build filename from properties
        filename = build_dxf_filename(part_name, thickness, props)
        
        # Export to DXF
        trans_id = initiate_translation(client, ctx, temp_drawing_id, 'DXF', part_name)
        result_id, _ = poll_translation(client, trans_id)
        
        # Rename the exported blob to match assembled filename
        rename_element(client, ctx, result_id, filename)
        
        logging.info(f"Exported '{part_name}' → {result_id} ({filename})")
        return (result_id, filename)
        
    finally:
        # Always clean up temp drawing
        try:
            delete_element(client, ctx, temp_drawing_id)
        except Exception as e:
            logging.warning(f"Failed to delete temp drawing: {e}")


def export_part_studio(
    client: OnshapeClient,
    ctx: DocContext,
    part_studio: Dict[str, Any]
) -> List[ExportResult]:
    """Export all parts from a Part Studio as DXFs.
    
    Two-phase export:
    1. Export flat patterns directly (sheet metal, already oriented)
    2. Export regular parts using 'Orient Plates for Export' feature
    
    Returns:
        List of (result_eid, filename) tuples for successful exports
    """
    eid = part_studio['id']
    name = part_studio['name']
    results: List[ExportResult] = []
    
    logging.info(f"Processing Part Studio: {name}")
    
    # Phase 1: Get all parts including flat patterns and categorize them
    all_parts = list_parts(client, ctx, eid, include_flat_parts=True)
    flat_patterns, regular_parts = categorize_parts(all_parts)
    
    logging.info(f"Found {len(flat_patterns)} flat patterns, {len(regular_parts)} regular parts")
    
    # Export flat patterns directly (they're already correctly oriented)
    for flat in flat_patterns:
        flat_name = flat.get('name', 'unnamed_flat')
        try:
            result = export_part_as_dxf(client, ctx, eid, flat)
            if result:
                results.append(result)
                logging.info(f"Exported flat pattern '{flat_name}'")
        except Exception as e:
            logging.error(f"Failed to export flat pattern '{flat_name}': {e}")
    
    # Phase 2: Export regular parts using orient feature (if any exist)
    if not regular_parts:
        logging.info(f"No regular parts to export in {name}")
        return results
    
    features = get_features(client, ctx, eid)
    orient_feature = find_orient_feature(features)
    
    if not orient_feature:
        logging.warning(f"No 'Orient Plates for Export' feature in {name}, skipping {len(regular_parts)} regular parts")
        return results
    
    # Unsuppress feature
    logging.info(f"Unsuppressing '{orient_feature.get('name')}'")
    update_feature_suppression(client, ctx, eid, orient_feature, False)
    
    try:
        time.sleep(5)  # Allow Part Studio to regenerate
        # Re-fetch parts after orient feature is unsuppressed
        oriented_parts = list_parts(client, ctx, eid)
        
        for part in oriented_parts:
            part_name = part.get('name', 'unnamed_part')
            try:
                result = export_part_as_dxf(client, ctx, eid, part)
                if result:
                    results.append(result)
            except Exception as e:
                logging.error(f"Failed to export part '{part_name}': {e}")
                
    finally:
        # Always re-suppress feature
        update_feature_suppression(client, ctx, eid, orient_feature, True)
        logging.info(f"Re-suppressed '{orient_feature.get('name')}'")
    
    return results


def export_drawing_as_pdf(
    client: OnshapeClient,
    ctx: DocContext,
    drawing: Dict[str, Any]
) -> Optional[ExportResult]:
    """Export an existing drawing as PDF.
    
    Returns:
        (result_eid, filename) tuple on success, None on failure
    """
    name = drawing['name']
    eid = drawing['id']
    
    logging.info(f"Processing drawing: {name}")
    
    try:
        # Get properties from the part referenced by this drawing
        props: PartProperties = {}
        missing: List[str] = []
        
        try:
            # Query drawing for referenced Part Studios/Assemblies
            refs = get_drawing_references(client, ctx, eid)
            
            if refs:
                # Get first unique targetElementId 
                ref = refs[0]
                target_eid = ref.get('targetElementId')
                
                if target_eid:
                    # Try to get element metadata directly (works for Assemblies)
                    try:
                        endpoint = f"/metadata{doc_path(ctx)}/e/{target_eid}"
                        metadata = client.request('GET', endpoint)
                        properties = metadata.get('properties', [])
                        prop_lookup = {p.get('propertyId'): p.get('value', '') for p in properties}
                        
                        if PROP_PART_NUMBER in prop_lookup and prop_lookup[PROP_PART_NUMBER]:
                            props['part_number'] = str(prop_lookup[PROP_PART_NUMBER])
                        else:
                            missing.append('Part Number')
                        
                        if PROP_REVISION in prop_lookup and prop_lookup[PROP_REVISION]:
                            props['revision'] = str(prop_lookup[PROP_REVISION])
                        else:
                            missing.append('Revision')
                            
                        if props:
                            logging.debug(f"Drawing '{name}' got properties from element {target_eid}")
                            
                    except Exception as e:
                        logging.debug(f"Failed to query element metadata: {e}")
                        missing = ['Part Number', 'Revision']
                else:
                    missing = ['Part Number', 'Revision']
            else:
                missing = ['Part Number', 'Revision']
                
        except Exception as e:
            logging.debug(f"Failed to get drawing references: {e}")
            missing = ['Part Number', 'Revision']
        
        if missing:
            logging.warning(f"Drawing '{name}' missing properties: {', '.join(missing)}")
        
        # Build filename from properties
        filename = build_pdf_filename(name, props)
        
        trans_id = initiate_translation(client, ctx, eid, 'PDF', name)
        result_id, _ = poll_translation(client, trans_id)
        
        # Rename the exported blob to match assembled filename
        rename_element(client, ctx, result_id, filename)
        
        logging.info(f"Exported '{name}' → {result_id} ({filename})")
        return (result_id, filename)
    except Exception as e:
        logging.error(f"Failed to export drawing '{name}': {e}")
        return None


def package_results(
    client: OnshapeClient,
    ctx: DocContext,
    results: List[ExportResult],
    output_dir: Path,
    log_entries: List[str]
) -> Tuple[Optional[Path], List[str]]:
    """Download exported files and package them into a ZIP.
    
    Detects filename collisions and skips duplicates, collecting warnings.
    
    Returns:
        Tuple of (path to ZIP file or None, list of collision warnings)
    """
    if not results:
        logging.info("No files to package")
        return None, []
    
    logging.info(f"Downloading {len(results)} files...")
    
    zip_name = f"onshape_export_{int(time.time())}.zip"
    zip_path = output_dir / zip_name
    
    seen_filenames: Dict[str, str] = {}  # filename -> first element_id
    collision_warnings: List[str] = []
    
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for result_id, filename in results:
            safe_name = filename.replace(' ', '_').replace('/', '_')
            
            # Check for filename collision
            if safe_name in seen_filenames:
                first_id = seen_filenames[safe_name]
                warning = f"Filename collision: '{safe_name}' - kept element {first_id}, skipped element {result_id}"
                collision_warnings.append(warning)
                logging.warning(warning)
                continue
            
            seen_filenames[safe_name] = result_id
            
            try:
                content = download_blob(client, ctx, result_id)
                zf.writestr(safe_name, content)
            except Exception as e:
                logging.error(f"Failed to download {result_id}: {e}")
        
        # Include log
        zf.writestr("export_operation.log", "\n".join(log_entries))
    
    logging.info(f"Created ZIP: {zip_path}")
    return zip_path, collision_warnings


def run_export_workflow(
    client: OnshapeClient,
    ctx: DocContext,
    output_dir: Path,
    clean_before: bool = False,
    clean_after: bool = False
) -> Optional[Path]:
    """Main export workflow orchestrator using pipeline composition.
    
    Steps:
    1. Cleanup leftover temp elements (and pre-clean if requested)
    2. Discover Part Studios and Drawings
    3. Export parts from Part Studios as DXFs
    4. Export Drawings as PDFs
    5. Package all results into a ZIP
    6. Post-clean if requested
    
    Args:
        clean_before: If True, delete existing DXF/PDF blobs before export
        clean_after: If True, delete new DXF/PDF blobs after packaging
    
    Returns:
        Path to the created ZIP file, or None on failure
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Step functions (pure: WorkflowState -> WorkflowState)
    def step_init(state: WorkflowState) -> WorkflowState:
        """Initialize workflow state."""
        return log_step(state, "Starting export workflow")
    
    def step_pre_clean(state: WorkflowState) -> WorkflowState:
        """Pre-clean: delete existing exports if requested."""
        if clean_before and is_mutable(state['ctx']):
            deleted = cleanup_exports(state['client'], state['ctx'])
            if deleted > 0:
                return log_step(state, f"Pre-cleaned {deleted} existing exports")
        return state
    
    def step_cleanup_temp(state: WorkflowState) -> WorkflowState:
        """Cleanup leftover temp elements."""
        cleanup_temp_elements(state['client'], state['ctx'])
        return state
    
    def step_discover(state: WorkflowState) -> WorkflowState:
        """Discover Part Studios and Drawings."""
        part_studios, drawings = discover_exportables(state['client'], state['ctx'])
        state = {**state, 'part_studios': part_studios, 'drawings': drawings}
        return log_step(state, f"Found {len(part_studios)} Part Studios, {len(drawings)} drawings")
    
    def step_export_dxfs(state: WorkflowState) -> WorkflowState:
        """Export parts from Part Studios as DXFs."""
        results = list(state.get('results', []))
        for ps in state.get('part_studios', []):
            ps_results = export_part_studio(state['client'], state['ctx'], ps)
            results.extend(ps_results)
            for _, filename in ps_results:
                state = log_step(state, f"Exported: {filename}")
        return {**state, 'results': results}
    
    def step_export_pdfs(state: WorkflowState) -> WorkflowState:
        """Export Drawings as PDFs."""
        results = list(state.get('results', []))
        for dr in state.get('drawings', []):
            result = export_drawing_as_pdf(state['client'], state['ctx'], dr)
            if result:
                results.append(result)
                state = log_step(state, f"Exported: {result[1]}")
        return {**state, 'results': results}
    
    def step_package(state: WorkflowState) -> WorkflowState:
        """Package results into ZIP."""
        results = state.get('results', [])
        log_entries = state.get('log_entries', [])
        zip_path, collision_warnings = package_results(
            state['client'], state['ctx'], results, state['output_dir'], log_entries
        )
        state = {**state, 'zip_path': zip_path, 'collision_warnings': collision_warnings}
        if zip_path:
            return log_step(state, f"SUCCESS: {zip_path}")
        else:
            return log_step(state, "No files were exported")
    
    def step_post_clean(state: WorkflowState) -> WorkflowState:
        """Post-clean: delete exports from document if requested."""
        if clean_after and is_mutable(state['ctx']):
            deleted = cleanup_exports(state['client'], state['ctx'])
            if deleted > 0:
                return log_step(state, f"Post-cleaned {deleted} exports from document")
        return state
    
    # Build initial state with injected dependencies
    initial_state: WorkflowState = {
        'client': client,
        'ctx': ctx,
        'output_dir': output_dir,
        'results': [],
        'log_entries': [],
        'part_studios': [],
        'drawings': [],
        'zip_path': None,
        'collision_warnings': []
    }
    
    try:
        # Compose and execute pipeline
        workflow = pipeline(
            step_init,
            step_pre_clean,
            step_cleanup_temp,
            step_discover,
            step_export_dxfs,
            step_export_pdfs,
            step_package,
            step_post_clean
        )
        
        final_state = workflow(initial_state)
        zip_path = final_state.get('zip_path')
        
        if zip_path:
            print(f"\n--- SUCCESS ---\nZIP file ready: {zip_path}\n")
            
            collision_warnings = final_state.get('collision_warnings', [])
            if collision_warnings:
                print("--- FILENAME COLLISIONS ---")
                print("The following files had duplicate names. First occurrence was kept, others skipped:")
                for warning in collision_warnings:
                    print(f"  • {warning}")
                print("\nPlease review your export rules to ensure unique filenames.\n")
        
        return zip_path
        
    except Exception as e:
        logging.error(f"Workflow failed: {e}")
        error_log = output_dir / "critical_error.log"
        with open(error_log, "w") as f:
            f.write(f"CRITICAL ERROR: {e}")
        return None


# ============================================================
# SECTION 7: CLI Entry Point
# ============================================================

def main():
    """Parse arguments, load config, and run the export workflow."""
    parser = argparse.ArgumentParser(description="Onshape Manufacturing Export Tool")
    parser.add_argument("--out", default="exports", help="Output directory")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--clean-before", action="store_true", 
                        help="Delete existing DXFs/PDFs before export (workspace only)")
    parser.add_argument("--clean-after", action="store_true", 
                        help="Delete DXFs/PDFs from document after packaging (workspace only)")
    parser.add_argument("--version-id", 
                        help="Export from version instead of workspace (read-only)")
    parser.add_argument("--setup", action="store_true",
                        help="Run interactive setup wizard")
    parser.add_argument("--interactive", action="store_true",
                        help="Interactively browse and select document to export")
    args = parser.parse_args()

    # Determine base directory (works for both script and packaged exe)
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent
    
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format=LOG_FORMAT)

    secrets_path = base_dir / ".secrets"
    config_path = base_dir / "config"
    output_path = base_dir / args.out
    
    # Handle --setup mode
    if args.setup:
        run_setup_wizard(secrets_path, config_path)
        return
    
    # Load or prompt for secrets
    secrets = get_or_prompt_secrets(secrets_path)
    client = OnshapeClient(secrets['access_key'], secrets['secret_key'])
    
    # Handle --interactive mode
    if args.interactive:
        run_interactive_workflow(client, output_path,
                                clean_before=args.clean_before,
                                clean_after=args.clean_after)
        return
    
    # Standard mode: load document config
    doc_config = load_document_config(config_path)
    if not doc_config:
        print("No document configuration found.")
        print(f"Run with {get_run_command()} --setup to configure, or --interactive to browse documents.")
        return
    
    did, wid = doc_config
    
    # Create context: use version if specified, otherwise workspace
    if args.version_id:
        ctx = make_version_context(did, args.version_id)
        logging.info(f"Exporting from version: {args.version_id}")
        if args.clean_before or args.clean_after:
            logging.warning("--clean-before/--clean-after ignored: cannot modify immutable version")
    else:
        ctx = make_context(did, wid)
    
    run_export_workflow(client, ctx, output_path, 
                        clean_before=args.clean_before, 
                        clean_after=args.clean_after)


if __name__ == "__main__":
    main()
