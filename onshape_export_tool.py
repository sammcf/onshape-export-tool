#!/usr/bin/env python3
import os
import json
import logging
import argparse
import requests
import time
import zipfile
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, Union, Callable, TypeVar, cast

# --- Configuration & Constants ---
API_BASE = "https://cad.onshape.com/api/v12"
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"

# Standard template IDs for Onshape ANSI A Inch (we use ISO in the call, but these are the base templates)
DEFAULT_TEMPLATE_DOC = "09fb14dcb55eee217f55fa7b"
DEFAULT_TEMPLATE_ELEMENT = "149ce62208ba05ac0cee75e5"

def load_config(path: Union[str, Path] = "config") -> Dict[str, Any]:
    """Loads JSON configuration."""
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logging.warning(f"Config file not found at {path}")
        return {}
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing config JSON: {e}")
        return {}


# --- Custom Exceptions ---
class TranslationError(Exception):
    """Raised when a translation job fails."""
    pass

class ElementNotFoundError(Exception):
    """Raised when an expected element disappears."""
    pass


# --- Generic Polling Infrastructure ---
T = TypeVar('T')

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

class OnshapeClient:
    def __init__(self, access_key: str, secret_key: str, base_url: str = API_BASE):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.auth = (access_key, secret_key)
        self.session.headers.update({
            'Accept': 'application/vnd.onshape.v1+json',
            'Content-Type': 'application/json'
        })

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
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

    def list_elements(self, did: str, wid: str) -> List[Dict[str, Any]]:
        endpoint = f"/documents/d/{did}/w/{wid}/elements"
        resp = self._request('GET', endpoint)
        return resp if isinstance(resp, list) else resp.get('elements', [])

    def get_features(self, did: str, wid: str, eid: str) -> List[Dict[str, Any]]:
        endpoint = f"/partstudios/d/{did}/w/{wid}/e/{eid}/features"
        resp = self._request('GET', endpoint)
        return resp.get('features', [])

    def update_feature_suppression(self, did: str, wid: str, eid: str, feature: Dict[str, Any], suppressed: bool):
        feature_id = feature.get('featureId')
        feature_copy = json.loads(json.dumps(feature))
        feature_copy['suppressed'] = suppressed
        
        payload = {
            "feature": feature_copy,
            "serializationVersion": "1.2.15",
            "sourceMicroversion": ""
        }
        endpoint = f"/partstudios/d/{did}/w/{wid}/e/{eid}/features/featureid/{feature_id}"
        return self._request('POST', endpoint, json=payload)

    def list_parts(self, did: str, wid: str, eid: str) -> List[Dict[str, Any]]:
        endpoint = f"/parts/d/{did}/w/{wid}/e/{eid}"
        return self._request('GET', endpoint)

    def create_empty_drawing(self, did: str, wid: str, name: str) -> str:
        """Create an empty temporary drawing."""
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
        logging.info(f"Creating empty temporary drawing '{name}'")
        resp = self._request('POST', endpoint, json=payload)
        return cast(str, resp.get('id'))

    def add_top_view_to_drawing(self, did: str, wid: str, eid: str, source_eid: str, part_id: str):
        """Add a 1:1 Top view of a part to the drawing."""
        endpoint = f"/drawings/d/{did}/w/{wid}/e/{eid}/modify"
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
        logging.info(f"Adding 1:1 Top view of part {part_id} to drawing {eid}")
        return self._request('POST', endpoint, json=payload)

    def wait_for_drawing_update(self, did: str, wid: str, eid: str, old_mv: Optional[str], timeout: int = 60) -> str:
        """Poll until the element's microversionId changes, indicating a successful modification."""
        logging.info(f"Waiting for drawing {eid} to update (polling microversion)...")
        
        def fetch():
            elements = self.list_elements(did, wid)
            return next((e for e in elements if e['id'] == eid), None)
        
        def check_microversion(element):
            if element is None:
                raise ElementNotFoundError(f"Element {eid} disappeared during modification!")
            new_mv = element.get('microversionId')
            if new_mv and new_mv != old_mv:
                logging.info(f"Drawing updated (MV: {new_mv})")
                return new_mv
            return None  # Keep polling
        
        result = poll_until(fetch, check_microversion, timeout)
        # Small buffer for drawing app to finish internal rendering
        time.sleep(2)
        return result

    def delete_element(self, did: str, wid: str, eid: str):
        """Delete an element from the document."""
        endpoint = f"/elements/d/{did}/w/{wid}/e/{eid}"
        logging.info(f"Deleting temporary element {eid}")
        return self._request('DELETE', endpoint)

    def initiate_translation(self, did: str, wid: str, eid: str, element_type: str, format_name: str, destination_name: str) -> str:
        """Initiate translation and store in document."""
        type_map = {
            'PARTSTUDIO': 'partstudios', 
            'DRAWING': 'drawings', 
            'ASSEMBLY': 'assemblies',
            'APPLICATION': 'drawings'
        }
        segment = type_map.get(element_type, 'partstudios')
        
        endpoint = f"/{segment}/d/{did}/w/{wid}/e/{eid}/translations"
        payload = {
            "formatName": format_name,
            "storeInDocument": True,
            "evaluateExportRule": True,
            "destinationName": destination_name,
            "includeFormedCentermarks": False
        }
        logging.info(f"Initiating {format_name} translation for {element_type} {eid}")
        resp = self._request('POST', endpoint, json=payload)
        return cast(str, resp.get('id'))

    def poll_translation(self, translation_id: str, timeout: int = 300) -> str:
        """Poll for translation completion and return the resulting element ID."""
        endpoint = f"/translations/{translation_id}"
        
        def fetch():
            return self._request('GET', endpoint)
        
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

    def download_blob(self, did: str, wid: str, eid: str, name: str) -> bytes:
        """Download blob content."""
        endpoint = f"/blobelements/d/{did}/w/{wid}/e/{eid}"
        logging.info(f"Downloading blob element {name} ({eid})")
        content = self._request('GET', endpoint)
        return cast(bytes, content)

    def run_full_workflow(self, config: Dict[str, Any], output_dir: Path):
        did = cast(str, config.get('documentId'))
        wid = cast(str, config.get('workspaceId'))
        
        if not did or not wid:
            logging.error("Missing documentId or workspaceId in config")
            return

        log_entries = []

        def log(msg, level=logging.INFO):
            logging.log(level, msg)
            log_entries.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}")

        output_dir.mkdir(parents=True, exist_ok=True)
        export_results = [] # List of (eid, name)

        try:
            elements = self.list_elements(did, wid)
            
            # Pre-flight Cleanup: Remove any existing temporary drawings or debug elements from previous runs
            cleanup_prefixes = ("TEMP_", "DEBUG_VIEW_", "TEST_MV_")
            temp_elements = [e for e in elements if e.get('name', '').startswith(cleanup_prefixes)]
            if temp_elements:
                log(f"Cleaning up {len(temp_elements)} leftover temporary or debug elements...")
                for e in temp_elements:
                    try:
                        self.delete_element(did, wid, e['id'])
                    except Exception as ex:
                        logging.debug(f"Failed to delete leftover {e['id']}: {ex}")
                # Refresh element list after cleanup
                elements = self.list_elements(did, wid)

            part_studios = [e for e in elements if e['elementType'] == 'PARTSTUDIO']
            drawings = [e for e in elements if e['elementType'] == 'DRAWING' or 
                        (e['elementType'] == 'APPLICATION' and 'drawing' in e.get('dataType', '').lower())]
            
            log(f"Discovered {len(part_studios)} Part Studios and {len(drawings)} drawings.")

            # Phase 1: Part Studios
            for ps in part_studios:
                eid = ps['id']
                log(f"Processing Part Studio: {ps['name']}")
                
                features = self.get_features(did, wid, eid)
                
                # Identify "Orient Plates for Export" features and select the one with the highest numeric index
                feature_pattern = re.compile(r"^Orient Plates for Export(?: (\d+))?$")
                orient_feature_candidates = []
                for f in features:
                    match = feature_pattern.match(f.get('name', ''))
                    if match:
                        num = int(match.group(1)) if match.group(1) else 0
                        orient_feature_candidates.append((num, f))
                
                if not orient_feature_candidates:
                    log(f"No 'Orient Plates for Export' feature found in {ps['name']}. Skipping.")
                    continue

                orient_feature_candidates.sort(key=lambda x: x[0], reverse=True)
                highest_index, orient_feature = orient_feature_candidates[0]
                
                log(f"Found feature '{orient_feature.get('name')}' (index {highest_index}). Unsuppressing it now.")
                self.update_feature_suppression(did, wid, eid, orient_feature, False)
                
                try:
                    time.sleep(5)
                    parts = self.list_parts(did, wid, eid)
                    log(f"Found {len(parts)} parts in oriented studio {ps['name']}")
                    
                    for part in parts:
                        part_id = cast(str, part.get('partId'))
                        part_name = cast(str, part.get('name', 'unnamed_part'))
                        
                        try:
                            # Create Temp Drawing (Empty)
                            temp_name = f"TEMP_{part_name}_{int(time.time())}"
                            temp_dr_id = self.create_empty_drawing(did, wid, temp_name)
                            
                            # Record current microversion to detect update
                            current_elements = self.list_elements(did, wid)
                            temp_el = next((e for e in current_elements if e['id'] == temp_dr_id), None)
                            old_mv = temp_el.get('microversionId') if temp_el else None
                            
                            log(f"Created empty temp drawing {temp_dr_id} for {part_name}")
                            
                            # Add 1:1 Top View
                            self.add_top_view_to_drawing(did, wid, temp_dr_id, eid, part_id)
                            
                            # Wait for drawing to process view creation
                            self.wait_for_drawing_update(did, wid, temp_dr_id, old_mv)
                            
                            try:
                                # Translate to DXF
                                trans_id = self.initiate_translation(did, wid, temp_dr_id, 'DRAWING', 'DXF', part_name)
                                res_id = self.poll_translation(trans_id)
                                log(f"Exported {part_name} to new tab {res_id}")
                                export_results.append((res_id, f"{part_name}.dxf"))
                            finally:
                                # Delete Temp Drawing
                                try:
                                    self.delete_element(did, wid, temp_dr_id)
                                except Exception as e:
                                    log(f"Warning: Failed to delete temp drawing {temp_dr_id}: {e}", logging.WARNING)
                        except Exception as e:
                            log(f"Error: Failed to process part {part_name}: {e}", logging.ERROR)
                            
                finally:
                    # Re-suppress
                    self.update_feature_suppression(did, wid, eid, orient_feature, True)
                    log(f"Operations complete. Successfully re-suppressed feature '{orient_feature.get('name')}'.")

            # Phase 2: Existing Drawings
            for dr in drawings:
                if dr['name'].startswith("TEMP_"):
                    continue
                    
                log(f"Processing existing drawing: {dr['name']} ({dr['id']})")
                try:
                    trans_id = self.initiate_translation(did, wid, dr['id'], 'DRAWING', 'PDF', dr['name'])
                    res_id = self.poll_translation(trans_id)
                    log(f"Exported drawing {dr['name']} to new tab {res_id}")
                    export_results.append((res_id, f"{dr['name']}.pdf"))
                except Exception as e:
                    log(f"Error: Failed to export drawing {dr['name']}: {e}", logging.ERROR)

            # Phase 3: Download and ZIP
            if export_results:
                log(f"Downloading {len(export_results)} exported files...")
                zip_name = f"onshape_export_{int(time.time())}.zip"
                zip_path = output_dir / zip_name
                
                with zipfile.ZipFile(zip_path, 'w') as zip_file:
                    all_elements = self.list_elements(did, wid)
                    element_names = {el['id']: el['name'] for el in all_elements}

                    for res_id, filename in export_results:
                        try:
                            actual_name = element_names.get(res_id, filename)
                            if filename.endswith('.dxf') and not actual_name.lower().endswith('.dxf'):
                                actual_name += '.dxf'
                            elif filename.endswith('.pdf') and not actual_name.lower().endswith('.pdf'):
                                actual_name += '.pdf'
                                
                            content = self.download_blob(did, wid, res_id, actual_name)
                            safe_name = actual_name.replace(' ', '_').replace('/', '_')
                            zip_file.writestr(safe_name, content)
                        except Exception as e:
                            log(f"Error: Failed to download result {res_id}: {e}", logging.ERROR)
                    
                    zip_file.writestr("export_operation.log", "\n".join(log_entries))
                
                log(f"SUCCESS: ZIP file created at {zip_path}")
                print(f"\n--- SUCCESS ---\nZIP file ready: {zip_path}\n")
            else:
                log("No files were successfully exported.")

        except Exception as e:
            log(f"CRITICAL: Workflow failed: {e}", logging.ERROR)
            with open(output_dir / "critical_error.log", "w") as f:
                f.write("\n".join(log_entries))
                f.write(f"\nCRITICAL ERROR: {e}")

def main():
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
    client.run_full_workflow(config, output_path)

if __name__ == "__main__":
    main()
