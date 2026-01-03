#!/usr/bin/env python3
"""Onshape Manufacturing Export Tool

Exports DXFs from oriented plate parts and PDFs from existing drawings.
Uses the Onshape REST API to automate the export workflow.
"""
import json
import logging
import argparse
import requests
import time
import zipfile
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Union, Callable, TypeVar, cast


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

def load_config(path: Union[str, Path] = "config") -> Dict[str, Any]:
    """Load JSON configuration from file."""
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logging.warning(f"Config file not found at {path}")
        return {}
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing config JSON: {e}")
        return {}


def poll_until(
    fetch: Callable[[], Any],
    predicate: Callable[[Any], Optional[T]],
    timeout: int = 60,
    interval: float = 2.0
) -> T:
    """Generic polling function.
    
    Args:
        fetch: Function that retrieves fresh data (e.g., API call)
        predicate: Function that examines data and returns a result when done,
                   or None to continue polling. May raise exceptions to abort.
        timeout: Maximum seconds to poll before raising TimeoutError
        interval: Seconds to wait between fetch calls
    
    Returns:
        The first non-None value returned by predicate
    
    Raises:
        TimeoutError: If timeout exceeded before predicate returns a value
    """
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
    """Authenticated HTTP client for Onshape API.
    
    This class handles only HTTP transport concerns:
    - Session management
    - Authentication
    - Request/response formatting
    
    All business logic is implemented as standalone functions that accept
    a client instance as their first parameter.
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
        """Make an authenticated API request.
        
        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            endpoint: API endpoint path (e.g., "/documents/d/{did}/w/{wid}/elements")
            **kwargs: Additional arguments passed to requests (json, params, etc.)
        
        Returns:
            Parsed JSON response (dict/list) or raw bytes for binary content
        
        Raises:
            requests.RequestException: On HTTP errors or connection failures
        """
        url = endpoint if endpoint.startswith('http') else f"{self.base_url}{endpoint}"
        try:
            logging.debug(f"API Request: {method} {url}")
            response = self.session.request(method, url, **kwargs)
            if response.status_code >= 400:
                logging.error(f"Error {response.status_code}: {response.text}")
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

def list_elements(client: OnshapeClient, did: str, wid: str) -> List[Dict[str, Any]]:
    """List all elements (tabs) in a document workspace."""
    endpoint = f"/documents/d/{did}/w/{wid}/elements"
    resp = client.request('GET', endpoint)
    return resp if isinstance(resp, list) else resp.get('elements', [])


def get_features(client: OnshapeClient, did: str, wid: str, eid: str) -> List[Dict[str, Any]]:
    """Get all features from a Part Studio."""
    endpoint = f"/partstudios/d/{did}/w/{wid}/e/{eid}/features"
    resp = client.request('GET', endpoint)
    return resp.get('features', [])


def list_parts(client: OnshapeClient, did: str, wid: str, eid: str) -> List[Dict[str, Any]]:
    """List all parts in a Part Studio."""
    endpoint = f"/parts/d/{did}/w/{wid}/e/{eid}"
    return client.request('GET', endpoint)


def update_feature_suppression(
    client: OnshapeClient, 
    did: str, 
    wid: str, 
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
    endpoint = f"/partstudios/d/{did}/w/{wid}/e/{eid}/features/featureid/{feature_id}"
    client.request('POST', endpoint, json=payload)


def delete_element(client: OnshapeClient, did: str, wid: str, eid: str) -> None:
    """Delete an element from the document."""
    endpoint = f"/elements/d/{did}/w/{wid}/e/{eid}"
    logging.info(f"Deleting element {eid}")
    client.request('DELETE', endpoint)


def create_drawing(client: OnshapeClient, did: str, wid: str, name: str) -> str:
    """Create an empty drawing. Returns the new element ID."""
    endpoint = f"/drawings/d/{did}/w/{wid}/create"
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
    did: str, 
    wid: str, 
    drawing_eid: str,
    source_eid: str, 
    part_id: str
) -> None:
    """Add a 1:1 top view of a part to a drawing."""
    endpoint = f"/drawings/d/{did}/w/{wid}/e/{drawing_eid}/modify"
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
    did: str, 
    wid: str, 
    eid: str, 
    format_name: str, 
    destination_name: str
) -> str:
    """Start a translation (export) job. Returns the translation ID."""
    endpoint = f"/drawings/d/{did}/w/{wid}/e/{eid}/translations"
    payload = {
        "formatName": format_name,
        "storeInDocument": True,
        "evaluateExportRule": True,
        "destinationName": destination_name,
        "includeFormedCentermarks": False
    }
    logging.info(f"Initiating {format_name} translation for element {eid}")
    resp = client.request('POST', endpoint, json=payload)
    return cast(str, resp.get('id'))


def poll_translation(client: OnshapeClient, translation_id: str, timeout: int = 300) -> str:
    """Poll until translation completes. Returns the result element ID."""
    endpoint = f"/translations/{translation_id}"
    
    def fetch():
        return client.request('GET', endpoint)
    
    def check_state(resp):
        state = resp.get('requestState')
        if state == 'DONE':
            ids = resp.get('resultElementIds', [])
            if ids:
                return ids[0]
            raise TranslationError("Translation done but no result element IDs found")
        elif state == 'FAILED':
            raise TranslationError(f"Translation failed: {resp.get('failureReason', 'Unknown reason')}")
        return None  # Keep polling
    
    return poll_until(fetch, check_state, timeout)


def wait_for_microversion_change(
    client: OnshapeClient, 
    did: str, 
    wid: str, 
    eid: str, 
    old_mv: Optional[str], 
    timeout: int = 60
) -> str:
    """Poll until an element's microversion changes. Returns the new microversion."""
    logging.info(f"Waiting for element {eid} to update...")
    
    def fetch():
        elements = list_elements(client, did, wid)
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


def download_blob(client: OnshapeClient, did: str, wid: str, eid: str) -> bytes:
    """Download blob element content as bytes."""
    endpoint = f"/blobelements/d/{did}/w/{wid}/e/{eid}"
    logging.debug(f"Downloading blob {eid}")
    return cast(bytes, client.request('GET', endpoint))


def get_element_microversion(client: OnshapeClient, did: str, wid: str, eid: str) -> Optional[str]:
    """Get the current microversion ID of an element."""
    elements = list_elements(client, did, wid)
    element = next((e for e in elements if e['id'] == eid), None)
    return element.get('microversionId') if element else None


# ============================================================
# SECTION 6: Business Logic (Workflow Orchestration)
# ============================================================

# This section will be refactored in Phase 3.
# For now, run_full_workflow remains but uses standalone functions.

def run_full_workflow(client: OnshapeClient, config: Dict[str, Any], output_dir: Path):
    """Main workflow: export DXFs from Part Studios and PDFs from Drawings."""
    did = cast(str, config.get('documentId'))
    wid = cast(str, config.get('workspaceId'))
    
    if not did or not wid:
        logging.error("Missing documentId or workspaceId in config")
        return

    log_entries: List[str] = []

    def log(msg: str, level: int = logging.INFO):
        logging.log(level, msg)
        log_entries.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}")

    output_dir.mkdir(parents=True, exist_ok=True)
    export_results: List[tuple] = []  # List of (eid, filename)

    try:
        elements = list_elements(client, did, wid)
        
        # Pre-flight Cleanup
        temp_elements = [e for e in elements if e.get('name', '').startswith(TEMP_ELEMENT_PREFIXES)]
        if temp_elements:
            log(f"Cleaning up {len(temp_elements)} leftover temporary elements...")
            for e in temp_elements:
                try:
                    delete_element(client, did, wid, e['id'])
                except Exception as ex:
                    logging.debug(f"Failed to delete leftover {e['id']}: {ex}")
            elements = list_elements(client, did, wid)

        # Categorize elements
        part_studios = [e for e in elements if e['elementType'] == 'PARTSTUDIO']
        drawings = [e for e in elements if e['elementType'] == 'DRAWING' or 
                    (e['elementType'] == 'APPLICATION' and 'drawing' in e.get('dataType', '').lower())]
        
        log(f"Discovered {len(part_studios)} Part Studios and {len(drawings)} drawings.")

        # Phase 1: Part Studios → DXF
        for ps in part_studios:
            eid = ps['id']
            log(f"Processing Part Studio: {ps['name']}")
            
            features = get_features(client, did, wid, eid)
            
            # Find "Orient Plates for Export" feature with highest index
            feature_pattern = re.compile(r"^Orient Plates for Export(?: (\d+))?$")
            orient_candidates = []
            for f in features:
                match = feature_pattern.match(f.get('name', ''))
                if match:
                    num = int(match.group(1)) if match.group(1) else 0
                    orient_candidates.append((num, f))
            
            if not orient_candidates:
                log(f"No 'Orient Plates for Export' feature found in {ps['name']}. Skipping.")
                continue

            orient_candidates.sort(key=lambda x: x[0], reverse=True)
            _, orient_feature = orient_candidates[0]
            
            log(f"Unsuppressing feature '{orient_feature.get('name')}'")
            update_feature_suppression(client, did, wid, eid, orient_feature, False)
            
            try:
                time.sleep(5)  # Allow Part Studio to regenerate
                parts = list_parts(client, did, wid, eid)
                log(f"Found {len(parts)} parts in {ps['name']}")
                
                for part in parts:
                    part_id = cast(str, part.get('partId'))
                    part_name = cast(str, part.get('name', 'unnamed_part'))
                    
                    try:
                        # Create temp drawing
                        temp_name = f"TEMP_{part_name}_{int(time.time())}"
                        temp_drawing_id = create_drawing(client, did, wid, temp_name)
                        old_mv = get_element_microversion(client, did, wid, temp_drawing_id)
                        log(f"Created temp drawing for {part_name}")
                        
                        # Add view and wait for it to render
                        add_view_to_drawing(client, did, wid, temp_drawing_id, eid, part_id)
                        wait_for_microversion_change(client, did, wid, temp_drawing_id, old_mv)
                        
                        try:
                            # Export to DXF
                            trans_id = initiate_translation(client, did, wid, temp_drawing_id, 'DXF', part_name)
                            result_id = poll_translation(client, trans_id)
                            log(f"Exported {part_name} → {result_id}")
                            export_results.append((result_id, f"{part_name}.dxf"))
                        finally:
                            # Always delete temp drawing
                            try:
                                delete_element(client, did, wid, temp_drawing_id)
                            except Exception as e:
                                log(f"Warning: Failed to delete temp drawing: {e}", logging.WARNING)
                    except Exception as e:
                        log(f"Error processing part {part_name}: {e}", logging.ERROR)
                        
            finally:
                # Re-suppress feature
                update_feature_suppression(client, did, wid, eid, orient_feature, True)
                log(f"Re-suppressed feature '{orient_feature.get('name')}'")

        # Phase 2: Existing Drawings → PDF
        for dr in drawings:
            if dr['name'].startswith("TEMP_"):
                continue
                
            log(f"Processing drawing: {dr['name']}")
            try:
                trans_id = initiate_translation(client, did, wid, dr['id'], 'PDF', dr['name'])
                result_id = poll_translation(client, trans_id)
                log(f"Exported {dr['name']} → {result_id}")
                export_results.append((result_id, f"{dr['name']}.pdf"))
            except Exception as e:
                log(f"Error exporting drawing {dr['name']}: {e}", logging.ERROR)

        # Phase 3: Download and ZIP
        if export_results:
            log(f"Downloading {len(export_results)} files...")
            zip_name = f"onshape_export_{int(time.time())}.zip"
            zip_path = output_dir / zip_name
            
            with zipfile.ZipFile(zip_path, 'w') as zf:
                all_elements = list_elements(client, did, wid)
                element_names = {el['id']: el['name'] for el in all_elements}

                for result_id, filename in export_results:
                    try:
                        actual_name = element_names.get(result_id, filename)
                        if filename.endswith('.dxf') and not actual_name.lower().endswith('.dxf'):
                            actual_name += '.dxf'
                        elif filename.endswith('.pdf') and not actual_name.lower().endswith('.pdf'):
                            actual_name += '.pdf'
                            
                        content = download_blob(client, did, wid, result_id)
                        safe_name = actual_name.replace(' ', '_').replace('/', '_')
                        zf.writestr(safe_name, content)
                    except Exception as e:
                        log(f"Error downloading {result_id}: {e}", logging.ERROR)
                
                zf.writestr("export_operation.log", "\n".join(log_entries))
            
            log(f"SUCCESS: ZIP created at {zip_path}")
            print(f"\n--- SUCCESS ---\nZIP file ready: {zip_path}\n")
        else:
            log("No files were successfully exported.")

    except Exception as e:
        log(f"CRITICAL: Workflow failed: {e}", logging.ERROR)
        with open(output_dir / "critical_error.log", "w") as f:
            f.write("\n".join(log_entries))
            f.write(f"\nCRITICAL ERROR: {e}")


# ============================================================
# SECTION 7: CLI Entry Point
# ============================================================

def main():
    """Parse arguments, load config, and run the export workflow."""
    parser = argparse.ArgumentParser(description="Onshape Manufacturing Export Tool")
    parser.add_argument("--out", default="exports", help="Output directory")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    base_dir = Path(__file__).parent.parent
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format=LOG_FORMAT)

    config = load_config(Path(__file__).parent / "config")
    access_key = cast(str, config.get('accessKey'))
    secret_key = cast(str, config.get('secretKey'))

    if not access_key or not secret_key:
        logging.critical("API credentials missing in config file.")
        return

    client = OnshapeClient(access_key, secret_key)
    output_path = base_dir / args.out
    run_full_workflow(client, config, output_path)


if __name__ == "__main__":
    main()
