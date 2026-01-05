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
from typing import Dict, Any, List, Optional, Tuple, Union, Callable, TypeVar, cast


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

# Type alias for export results: (element_id, filename)
ExportResult = Tuple[str, str]

# Type alias for translation results: (element_id, export_rule_filename or None)
TranslationResult = Tuple[str, Optional[str]]


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


def list_parts(
    client: OnshapeClient, did: str, wid: str, eid: str,
    include_flat_parts: bool = False
) -> List[Dict[str, Any]]:
    """List all parts in a Part Studio."""
    endpoint = f"/parts/d/{did}/w/{wid}/e/{eid}"
    params = {}
    if include_flat_parts:
        params['includeFlatParts'] = 'true'
    return client.request('GET', endpoint, params=params)


def categorize_parts(parts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Separate flat patterns from regular parts.
    
    Flat patterns (sheet metal) should be exported directly without orient transformation.
    Regular parts that have flat patterns are filtered out (the flat is preferred).
    
    Returns:
        (flat_patterns, regular_parts)
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
# SECTION 6: Business Logic (Workflow Functions)
# ============================================================

# Each function has a single responsibility and can be composed
# to create different export workflows.

def cleanup_temp_elements(client: OnshapeClient, did: str, wid: str) -> int:
    """Delete any leftover temporary elements from previous runs.
    
    Returns:
        Number of elements deleted
    """
    elements = list_elements(client, did, wid)
    temp_elements = [e for e in elements if e.get('name', '').startswith(TEMP_ELEMENT_PREFIXES)]
    
    deleted = 0
    for e in temp_elements:
        try:
            delete_element(client, did, wid, e['id'])
            deleted += 1
        except Exception as ex:
            logging.debug(f"Failed to delete leftover {e['id']}: {ex}")
    
    if deleted > 0:
        logging.info(f"Cleaned up {deleted} temporary elements")
    return deleted


def discover_exportables(
    client: OnshapeClient, 
    did: str, 
    wid: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Categorize document elements into Part Studios and Drawings.
    
    Returns:
        Tuple of (part_studios, drawings) where each is a list of element dicts
    """
    elements = list_elements(client, did, wid)
    
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
    did: str,
    wid: str,
    part_studio_eid: str,
    part: Dict[str, Any]
) -> Optional[ExportResult]:
    """Export a single part as DXF via temporary drawing.
    
    Creates a temp drawing, adds a top view, exports to DXF, then cleans up.
    
    Returns:
        (result_element_id, filename) tuple on success, None on failure
    """
    part_id = cast(str, part.get('partId'))
    part_name = cast(str, part.get('name', 'unnamed_part'))
    
    # Create temp drawing
    temp_name = f"TEMP_{part_name}_{int(time.time())}"
    temp_drawing_id = create_drawing(client, did, wid, temp_name)
    old_mv = get_element_microversion(client, did, wid, temp_drawing_id)
    logging.info(f"Created temp drawing for '{part_name}'")
    
    try:
        # Add view and wait for it to render
        add_view_to_drawing(client, did, wid, temp_drawing_id, part_studio_eid, part_id)
        wait_for_microversion_change(client, did, wid, temp_drawing_id, old_mv)
        
        # Export to DXF
        trans_id = initiate_translation(client, did, wid, temp_drawing_id, 'DXF', part_name)
        result_id, export_rule_filename = poll_translation(client, trans_id)
        
        # Use export rule filename if available, otherwise fall back to part name
        filename = export_rule_filename or f"{part_name}.dxf"
        if not filename.lower().endswith('.dxf'):
            filename += '.dxf'
        
        logging.info(f"Exported '{part_name}' → {result_id} ({filename})")
        return (result_id, filename)
        
    finally:
        # Always clean up temp drawing
        try:
            delete_element(client, did, wid, temp_drawing_id)
        except Exception as e:
            logging.warning(f"Failed to delete temp drawing: {e}")


def export_part_studio(
    client: OnshapeClient,
    did: str,
    wid: str,
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
    all_parts = list_parts(client, did, wid, eid, include_flat_parts=True)
    flat_patterns, regular_parts = categorize_parts(all_parts)
    
    logging.info(f"Found {len(flat_patterns)} flat patterns, {len(regular_parts)} regular parts")
    
    # Export flat patterns directly (they're already correctly oriented)
    for flat in flat_patterns:
        flat_name = flat.get('name', 'unnamed_flat')
        try:
            result = export_part_as_dxf(client, did, wid, eid, flat)
            if result:
                results.append(result)
                logging.info(f"Exported flat pattern '{flat_name}'")
        except Exception as e:
            logging.error(f"Failed to export flat pattern '{flat_name}': {e}")
    
    # Phase 2: Export regular parts using orient feature (if any exist)
    if not regular_parts:
        logging.info(f"No regular parts to export in {name}")
        return results
    
    features = get_features(client, did, wid, eid)
    orient_feature = find_orient_feature(features)
    
    if not orient_feature:
        logging.warning(f"No 'Orient Plates for Export' feature in {name}, skipping {len(regular_parts)} regular parts")
        return results
    
    # Unsuppress feature
    logging.info(f"Unsuppressing '{orient_feature.get('name')}'")
    update_feature_suppression(client, did, wid, eid, orient_feature, False)
    
    try:
        time.sleep(5)  # Allow Part Studio to regenerate
        # Re-fetch parts after orient feature is unsuppressed
        oriented_parts = list_parts(client, did, wid, eid)
        
        for part in oriented_parts:
            part_name = part.get('name', 'unnamed_part')
            try:
                result = export_part_as_dxf(client, did, wid, eid, part)
                if result:
                    results.append(result)
            except Exception as e:
                logging.error(f"Failed to export part '{part_name}': {e}")
                
    finally:
        # Always re-suppress feature
        update_feature_suppression(client, did, wid, eid, orient_feature, True)
        logging.info(f"Re-suppressed '{orient_feature.get('name')}'")
    
    return results


def export_drawing_as_pdf(
    client: OnshapeClient,
    did: str,
    wid: str,
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
        trans_id = initiate_translation(client, did, wid, eid, 'PDF', name)
        result_id, export_rule_filename = poll_translation(client, trans_id)
        
        # Use export rule filename if available, otherwise fall back to drawing name
        filename = export_rule_filename or f"{name}.pdf"
        if not filename.lower().endswith('.pdf'):
            filename += '.pdf'
        
        logging.info(f"Exported '{name}' → {result_id} ({filename})")
        return (result_id, filename)
    except Exception as e:
        logging.error(f"Failed to export drawing '{name}': {e}")
        return None


def package_results(
    client: OnshapeClient,
    did: str,
    wid: str,
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
                content = download_blob(client, did, wid, result_id)
                zf.writestr(safe_name, content)
            except Exception as e:
                logging.error(f"Failed to download {result_id}: {e}")
        
        # Include log
        zf.writestr("export_operation.log", "\n".join(log_entries))
    
    logging.info(f"Created ZIP: {zip_path}")
    return zip_path, collision_warnings


def run_export_workflow(
    client: OnshapeClient,
    did: str,
    wid: str,
    output_dir: Path
) -> Optional[Path]:
    """Main export workflow orchestrator.
    
    Steps:
    1. Cleanup leftover temp elements
    2. Discover Part Studios and Drawings
    3. Export parts from Part Studios as DXFs
    4. Export Drawings as PDFs
    5. Package all results into a ZIP
    
    Returns:
        Path to the created ZIP file, or None on failure
    """
    log_entries: List[str] = []
    
    def log(msg: str):
        logging.info(msg)
        log_entries.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}")
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # Step 1: Cleanup
        cleanup_temp_elements(client, did, wid)
        
        # Step 2: Discovery
        part_studios, drawings = discover_exportables(client, did, wid)
        log(f"Found {len(part_studios)} Part Studios, {len(drawings)} drawings")
        
        # Step 3: Export Part Studios → DXF
        results: List[ExportResult] = []
        for ps in part_studios:
            ps_results = export_part_studio(client, did, wid, ps)
            results.extend(ps_results)
            for _, filename in ps_results:
                log(f"Exported: {filename}")
        
        # Step 4: Export Drawings → PDF
        for dr in drawings:
            result = export_drawing_as_pdf(client, did, wid, dr)
            if result:
                results.append(result)
                log(f"Exported: {result[1]}")
        
        # Step 5: Package
        zip_path, collision_warnings = package_results(client, did, wid, results, output_dir, log_entries)
        
        if zip_path:
            log(f"SUCCESS: {zip_path}")
            print(f"\n--- SUCCESS ---\nZIP file ready: {zip_path}\n")
            
            # Display collision warnings if any
            if collision_warnings:
                print("--- FILENAME COLLISIONS ---")
                print("The following files had duplicate names. First occurrence was kept, others skipped:")
                for warning in collision_warnings:
                    print(f"  • {warning}")
                print("\nPlease review your export rules to ensure unique filenames.\n")
            
            return zip_path
        else:
            log("No files were exported")
            return None
            
    except Exception as e:
        logging.error(f"Workflow failed: {e}")
        error_log = output_dir / "critical_error.log"
        with open(error_log, "w") as f:
            f.write("\n".join(log_entries))
            f.write(f"\nCRITICAL ERROR: {e}")
        return None


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
    
    # Validate required config
    access_key = config.get('accessKey')
    secret_key = config.get('secretKey')
    did = config.get('documentId')
    wid = config.get('workspaceId')

    if not access_key or not secret_key:
        logging.critical("API credentials missing in config file.")
        return
    
    if not did or not wid:
        logging.critical("Missing documentId or workspaceId in config file.")
        return

    client = OnshapeClient(access_key, secret_key)
    output_path = base_dir / args.out
    run_export_workflow(client, did, wid, output_path)


if __name__ == "__main__":
    main()
