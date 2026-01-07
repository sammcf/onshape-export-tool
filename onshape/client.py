"""Onshape API client and operations.

HTTP transport and all API operations for interacting with Onshape.
"""
import json
import logging
import time
import requests
from typing import Dict, Any, List, Optional, Tuple, Callable, TypeVar, cast
from typing_extensions import TypedDict


# --- Configuration & Constants ---

API_BASE = "https://cad.onshape.com/api/v12"

# Default drawing template (Onshape's ANSI A template, used with ISO settings)
DEFAULT_TEMPLATE_DOC = "09fb14dcb55eee217f55fa7b"
DEFAULT_TEMPLATE_ELEMENT = "149ce62208ba05ac0cee75e5"

# Onshape property IDs for metadata lookup
PROP_PART_NUMBER = "57f3fb8efa3416c06701d60f"
PROP_REVISION = "57f3fb8efa3416c06701d610"
PROP_MATERIAL = "57f3fb8efa3416c06701d615"

# Type alias for export results: (element_id, filename)
ExportResult = Tuple[str, str]

T = TypeVar('T')


# --- Document Context ---

class DocContext(TypedDict):
    """Bundles document/workspace/version IDs for API path construction."""
    did: str
    wvm_type: str  # 'w' = workspace, 'v' = version, 'm' = microversion
    wvm_id: str


def doc_path(ctx: DocContext, suffix: str = "") -> str:
    return f"/d/{ctx['did']}/{ctx['wvm_type']}/{ctx['wvm_id']}{suffix}"


def is_mutable(ctx: DocContext) -> bool:
    return ctx['wvm_type'] == 'w'


def make_workspace_context(did: str, wid: str) -> DocContext:
    return DocContext(did=did, wvm_type='w', wvm_id=wid)


def make_version_context(did: str, vid: str) -> DocContext:
    return DocContext(did=did, wvm_type='v', wvm_id=vid)


# --- Polling Utility ---

def poll_until(
    fetch: Callable[[], Any],
    predicate: Callable[[Any], Optional[T]],
    timeout: int = 60,
    interval: float = 2.0
) -> Optional[T]:
    """Poll until predicate returns non-None."""
    import time
    start = time.time()
    while time.time() - start < timeout:
        result = fetch()
        if result is not None:
            match = predicate(result)
            if match is not None:
                return match
        time.sleep(interval)
    logging.warning(f"Poll timed out after {timeout}s")
    return None


# --- OnshapeClient (Transport Only) ---

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


# --- API Operations ---

def list_elements(client: OnshapeClient, ctx: DocContext) -> List[Dict[str, Any]]:
    endpoint = f"/documents{doc_path(ctx)}/elements"
    resp = client.request('GET', endpoint)
    return resp if isinstance(resp, list) else resp.get('elements', [])


def get_features(client: OnshapeClient, ctx: DocContext, eid: str) -> List[Dict[str, Any]]:
    endpoint = f"/partstudios{doc_path(ctx)}/e/{eid}/features"
    resp = client.request('GET', endpoint)
    return resp.get('features', [])


def list_parts(
    client: OnshapeClient, ctx: DocContext, eid: str,
    include_flat_parts: bool = False
) -> List[Dict[str, Any]]:
    endpoint = f"/parts{doc_path(ctx)}/e/{eid}"
    params = {}
    if include_flat_parts:
        params['includeFlatParts'] = 'true'
    return client.request('GET', endpoint, params=params)


def categorize_parts(parts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Separate flat patterns (export directly) from regular parts."""
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
    
    # Filter out original sheet metal parts (they have flat patterns)
    regular_parts = [p for p in regular_parts if p.get('partId') not in flat_part_originals]
    
    return flat_patterns, regular_parts


def get_part_metadata(
    client: OnshapeClient, ctx: DocContext, eid: str, part_id: str
) -> Dict[str, Any]:
    endpoint = f"/metadata{doc_path(ctx)}/e/{eid}/p/{part_id}"
    return client.request('GET', endpoint)


def get_part_bounding_box(
    client: OnshapeClient, ctx: DocContext, eid: str, part_id: str
) -> Dict[str, float]:
    endpoint = f"/parts{doc_path(ctx)}/e/{eid}/partid/{part_id}/boundingboxes"
    return client.request('GET', endpoint)


def get_part_thickness(
    client: OnshapeClient, ctx: DocContext, eid: str, part_id: str
) -> Optional[float]:
    """Returns thickness in mm from bounding box Z-height. Assumes part is oriented face-normal parallel to z-axis."""
    try:
        bbox = get_part_bounding_box(client, ctx, eid, part_id)
        z_height = abs(bbox.get('highZ', 0) - bbox.get('lowZ', 0))
        thickness_mm = z_height * 1000  # Bounding box is in meters
        
        if thickness_mm > 0.01:
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


def extract_properties_from_lookup(
    prop_lookup: Dict[str, Any],
    include_material: bool = True
) -> Tuple[PartProperties, List[str]]:
    """Core extraction from property lookup dict. Returns (props, missing)."""
    props: PartProperties = {}
    missing: List[str] = []
    
    # Part Number
    if PROP_PART_NUMBER in prop_lookup and prop_lookup[PROP_PART_NUMBER]:
        props['part_number'] = str(prop_lookup[PROP_PART_NUMBER])
    else:
        missing.append('Part Number')
    
    # Revision
    if PROP_REVISION in prop_lookup and prop_lookup[PROP_REVISION]:
        props['revision'] = str(prop_lookup[PROP_REVISION])
    else:
        missing.append('Revision')
    
    # Material (optional, for parts only)
    if include_material:
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
    
    return props, missing


def get_element_properties(
    client: OnshapeClient, ctx: DocContext, eid: str
) -> Tuple[PartProperties, List[str]]:
    """Fetch properties from element metadata (no material)."""
    try:
        endpoint = f"/metadata{doc_path(ctx)}/e/{eid}"
        metadata = client.request('GET', endpoint)
        properties = metadata.get('properties', [])
        
        prop_lookup = {p.get('propertyId'): p.get('value', '') for p in properties}
        return extract_properties_from_lookup(prop_lookup, include_material=False)
        
    except Exception as e:
        logging.warning(f"Failed to get properties for element {eid}: {e}")
        return {}, ['Part Number', 'Revision']


def get_part_properties(
    client: OnshapeClient, ctx: DocContext, eid: str, part_id: str
) -> Tuple[PartProperties, List[str]]:
    """Fetch properties from part metadata (includes material)."""
    try:
        metadata = get_part_metadata(client, ctx, eid, part_id)
        properties = metadata.get('properties', [])
        
        prop_lookup = {p.get('propertyId'): p.get('value', '') for p in properties}
        return extract_properties_from_lookup(prop_lookup, include_material=True)
            
    except Exception as e:
        logging.warning(f"Failed to get properties for part {part_id}: {e}")
        return {}, ['Part Number', 'Revision', 'Material']


class FilenameSchema(TypedDict, total=False):
    include_thickness: bool
    include_material: bool
    extension: str


def build_export_filename(
    fallback_name: str,
    props: PartProperties,
    extension: str,
    thickness_mm: Optional[float] = None,
    include_material: bool = False
) -> str:
    """Build filename from properties with fallback to provided name."""
    part_number = props.get('part_number', '')
    revision = props.get('revision', '')
    material = props.get('material', '') if include_material else ''
    thickness_str = format_thickness_prefix(thickness_mm) if thickness_mm else ''
    
    # Build filename if we have required properties
    if part_number and revision:
        parts = []
        if thickness_str:
            parts.append(thickness_str)
        if material:
            parts.append(material)
        
        # Core: partNumber_Rev revision
        core = f"{part_number}_Rev {revision}"
        
        if parts:
            prefix = ' '.join(parts)
            return f"{prefix}_{core}.{extension}"
        return f"{core}.{extension}"
    
    # Fallback: use provided name with optional thickness prefix
    if thickness_str:
        return f"{thickness_str}{fallback_name}.{extension}"
    
    # Ensure we don't double the extension
    if fallback_name.lower().endswith(f'.{extension}'):
        return fallback_name
    return f"{fallback_name}.{extension}"


def build_dxf_filename(
    part_name: str,
    thickness_mm: Optional[float],
    props: PartProperties
) -> str:
    """DXF: {thickness}mm {material}_{partNumber}_Rev {revision}.dxf"""
    return build_export_filename(
        fallback_name=part_name,
        props=props,
        extension='dxf',
        thickness_mm=thickness_mm,
        include_material=True
    )


def build_pdf_filename(name: str, props: PartProperties) -> str:
    """PDF: {partNumber}_Rev {revision}.pdf"""
    return build_export_filename(
        fallback_name=name,
        props=props,
        extension='pdf',
        thickness_mm=None,
        include_material=False
    )


def update_feature_suppression(
    client: OnshapeClient, 
    ctx: DocContext,
    eid: str, 
    feature: Dict[str, Any], 
    suppressed: bool
) -> None:
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
    endpoint = f"/elements{doc_path(ctx)}/e/{eid}"
    logging.info(f"Deleting element {eid}")
    client.request('DELETE', endpoint)


def rename_element(client: OnshapeClient, ctx: DocContext, eid: str, new_name: str) -> None:
    """Rename element to match assembled filename."""
    endpoint = f"/metadata{doc_path(ctx)}/e/{eid}"
    
    try:
        # First, get the element's metadata to find the Name propertyId
        metadata = client.request('GET', endpoint)
        properties = metadata.get('properties', [])
        
        # Find the Name property
        name_prop_id = None
        for prop in properties:
            if prop.get('name') == 'Name':
                name_prop_id = prop.get('propertyId')
                break
        
        if not name_prop_id:
            logging.warning(f"Could not find Name propertyId for element {eid}")
            logging.debug(f"Available properties: {[p.get('name') for p in properties]}")
            return
        
        # Update the name
        payload = {
            "properties": [
                {"propertyId": name_prop_id, "value": new_name}
            ]
        }
        logging.debug(f"Renaming element {eid} to '{new_name}' using propertyId {name_prop_id}")
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


def poll_translation(client: OnshapeClient, translation_id: str, timeout: int = 300) -> Optional[str]:
    """Poll until translation completes. Returns element_id or None."""
    endpoint = f"/translations/{translation_id}"
    
    def fetch() -> Optional[Dict[str, Any]]:
        try:
            return client.request('GET', endpoint)
        except Exception as e:
            logging.error(f"Failed to poll translation {translation_id}: {e}")
            return None
    
    error_occurred = [False]
    
    def check_state(resp: Dict[str, Any]) -> Optional[str]:
        state = resp.get('requestState')
        if state == 'DONE':
            ids = resp.get('resultElementIds', [])
            if ids:
                return ids[0]
            logging.error("Translation done but no result element IDs found")
            error_occurred[0] = True
            return '__ERROR__'
        elif state == 'FAILED':
            logging.error(f"Translation failed: {resp.get('failureReason', 'Unknown reason')}")
            error_occurred[0] = True
            return '__ERROR__'
        return None  # Keep polling
    
    result = poll_until(fetch, check_state, timeout)
    if result is None or error_occurred[0]:
        return None
    return result


def execute_translation(
    client: OnshapeClient,
    ctx: DocContext,
    eid: str,
    format_name: str,
    destination_name: str,
    final_filename: str
) -> Optional[ExportResult]:
    """Initiate → poll → rename. Returns (element_id, filename) or None."""
    trans_id = initiate_translation(client, ctx, eid, format_name, destination_name)
    if not trans_id:
        logging.error(f"Failed to initiate {format_name} translation for element {eid}")
        return None
    
    result_id = poll_translation(client, trans_id)
    if result_id is None:
        logging.error(f"{format_name} translation failed for element {eid}")
        return None
    
    rename_element(client, ctx, result_id, final_filename)
    return (result_id, final_filename)


def wait_for_microversion_change(
    client: OnshapeClient, 
    ctx: DocContext,
    eid: str, 
    old_mv: Optional[str], 
    timeout: int = 60
) -> Optional[str]:
    """Poll until element microversion changes. Returns new mv or None."""
    logging.info(f"Waiting for element {eid} to update...")
    
    def fetch() -> Optional[Dict[str, Any]]:
        try:
            elements = list_elements(client, ctx)
            return next((e for e in elements if e['id'] == eid), None)
        except Exception as e:
            logging.error(f"Failed to fetch elements: {e}")
            return None
    
    def check_microversion(element: Optional[Dict[str, Any]]) -> Optional[str]:
        if element is None:
            logging.error(f"Element {eid} not found")
            return '__NOT_FOUND__'  # Sentinel to stop polling
        new_mv = element.get('microversionId')
        if new_mv and new_mv != old_mv:
            logging.info(f"Element updated (MV: {new_mv})")
            return new_mv
        return None  # Keep polling
    
    result = poll_until(fetch, check_microversion, timeout)
    if result is None or result == '__NOT_FOUND__':
        return None
    # Small buffer for drawing app to finish internal rendering
    time.sleep(2)
    return result


def download_blob(client: OnshapeClient, ctx: DocContext, eid: str) -> Optional[bytes]:
    endpoint = f"/blobelements{doc_path(ctx)}/e/{eid}"
    logging.debug(f"Downloading blob {eid}")
    try:
        return cast(bytes, client.request('GET', endpoint))
    except Exception as e:
        logging.error(f"Failed to download blob {eid}: {e}")
        return None


def get_element_microversion(client: OnshapeClient, ctx: DocContext, eid: str) -> Optional[str]:
    """Get the current microversion ID of an element."""
    elements = list_elements(client, ctx)
    element = next((e for e in elements if e['id'] == eid), None)
    return element.get('microversionId') if element else None


# --- Document Browsing API ---

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
