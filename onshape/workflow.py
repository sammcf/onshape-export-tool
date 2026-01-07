"""Export workflow business logic.

Contains the main export orchestration and all workflow steps.
"""
import logging
import re
import time
import zipfile
from functools import reduce
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable, TypeVar, cast
from typing_extensions import TypedDict

from .client import (
    OnshapeClient, DocContext, ExportResult,
    is_mutable, list_elements, get_features, list_parts, categorize_parts,
    get_part_thickness, get_part_properties, get_element_properties,
    build_dxf_filename, build_pdf_filename,
    update_feature_suppression, delete_element, create_drawing,
    add_view_to_drawing, get_element_microversion, wait_for_microversion_change,
    execute_translation, download_blob, get_drawing_references, PartProperties
)


# Prefixes for temporary elements that should be cleaned up
TEMP_ELEMENT_PREFIXES = ("TEMP_", "DEBUG_VIEW_", "TEST_MV_")

T = TypeVar('T')


def pipeline(*steps: Callable[[T], T]) -> Callable[[T], T]:
    """Compose left-to-right: pipeline(f, g, h)(x) = h(g(f(x)))"""
    return lambda initial: reduce(lambda state, step: step(state), steps, initial)


class WorkflowState(TypedDict, total=False):
    """Immutable state for pipeline. Each step returns {**state, 'key': new}."""
    client: Any
    ctx: DocContext
    output_dir: Path
    results: List[ExportResult]
    log_entries: List[str]
    part_studios: List[Dict[str, Any]]
    drawings: List[Dict[str, Any]]
    zip_path: Optional[Path]
    collision_warnings: List[str]


def log_step(state: WorkflowState, msg: str) -> WorkflowState:
    entries = list(state.get('log_entries', []))
    entries.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}")
    logging.info(msg)
    return {**state, 'log_entries': entries}


# --- Cleanup Functions ---

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


# --- Discovery Functions ---

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


# --- Export Functions ---

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
    if not temp_drawing_id:
        logging.error(f"Failed to create temp drawing for '{part_name}'")
        return None
    
    old_mv = get_element_microversion(client, ctx, temp_drawing_id)
    logging.info(f"Created temp drawing for '{part_name}'")
    
    try:
        # Add view and wait for it to render
        add_view_to_drawing(client, ctx, temp_drawing_id, part_studio_eid, part_id)
        new_mv = wait_for_microversion_change(client, ctx, temp_drawing_id, old_mv)
        if new_mv is None:
            logging.error(f"Timed out waiting for view to render for '{part_name}'")
            return None
        
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
        
        # Export to DXF using unified translation
        result = execute_translation(client, ctx, temp_drawing_id, 'DXF', part_name, filename)
        if result is None:
            return None
        
        logging.info(f"Exported '{part_name}' → {result[0]} ({filename})")
        return result
        
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
    
    # Get properties from the element referenced by this drawing
    props: PartProperties = {}
    missing: List[str] = ['Part Number', 'Revision']
    
    # Query drawing for referenced Part Studios/Assemblies
    refs = get_drawing_references(client, ctx, eid)
    
    if refs:
        ref = refs[0]
        target_eid = ref.get('targetElementId')
        
        if target_eid:
            props, missing = get_element_properties(client, ctx, target_eid)
            if props:
                logging.debug(f"Drawing '{name}' got properties from element {target_eid}")
    
    if missing:
        logging.warning(f"Drawing '{name}' missing properties: {', '.join(missing)}")
    
    # Build filename from properties
    filename = build_pdf_filename(name, props)
    
    # Export to PDF using unified translation
    result = execute_translation(client, ctx, eid, 'PDF', name, filename)
    if result is None:
        return None
    
    logging.info(f"Exported '{name}' → {result[0]} ({filename})")
    return result


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
            
            content = download_blob(client, ctx, result_id)
            if content is None:
                logging.error(f"Failed to download {result_id}, skipping")
                continue
            zf.writestr(safe_name, content)
        
        # Include log
        zf.writestr("export_operation.log", "\n".join(log_entries))
    
    logging.info(f"Created ZIP: {zip_path}")
    return zip_path, collision_warnings


# --- Main Workflow ---

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
