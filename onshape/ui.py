"""Terminal UI primitives and interactive flows.

Reusable UI components for consistent interactive experience.
"""
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable

from .secrets import (
    load_secrets, save_secrets, prompt_secrets, get_or_prompt_secrets,
    save_document_config, prompt_document_config
)


# --- UI Primitives ---

def print_header(title: str, width: int = 60) -> None:
    """Print a centered header banner."""
    print("\n" + "=" * width)
    print(f"    {title}")
    print("=" * width)


def print_section(title: str, width: int = 40) -> None:
    """Print a section divider."""
    print(f"\n{title}")
    print("-" * width)


def interactive_select(items: List[Dict[str, Any]], prompt: str, 
                       display_fn: Callable[[Dict[str, Any]], str]) -> Optional[Dict[str, Any]]:
    """Numbered menu. Returns None on cancel or empty list."""
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


def interactive_menu(options: List[str], prompt: str = "Select option:") -> Optional[int]:
    """Lightweight numbered menu for simple string options.
    
    Returns 0-indexed choice, or None on cancel (0 input).
    """
    print(f"\n{prompt}\n")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    print("  0. Cancel\n")
    
    while True:
        try:
            choice = input("Enter number: ").strip()
            if not choice:
                continue
            idx = int(choice)
            if idx == 0:
                return None
            if 1 <= idx <= len(options):
                return idx - 1
            print(f"Please enter 1-{len(options)} or 0 to cancel")
        except ValueError:
            print("Please enter a number")


def interactive_toggles(options: Dict[str, bool], prompt: str = "Toggle options:") -> Dict[str, bool]:
    """Multi-select toggle menu for boolean options.
    
    Args:
        options: Dict mapping option name -> current value (True/False)
        prompt: Header text
        
    Returns:
        Updated dict with toggled values
    """
    result = dict(options)
    option_names = list(result.keys())
    
    while True:
        print(f"\n{prompt}\n")
        for i, name in enumerate(option_names, 1):
            status = "[X]" if result[name] else "[ ]"
            print(f"  {i}. {status} {name}")
        print("\n  Enter number to toggle, or press Enter to continue\n")
        
        choice = input("Enter: ").strip().lower()
        if choice == '':
            return result
        
        try:
            idx = int(choice)
            if 1 <= idx <= len(option_names):
                key = option_names[idx - 1]
                result[key] = not result[key]
        except ValueError:
            print("Enter a number or press Enter to continue")


# --- Setup Wizard ---

def run_setup_wizard(secrets_path: Path, config_path: Path) -> None:
    from .secrets import load_secrets, save_secrets, prompt_secrets
    from .secrets import save_document_config, prompt_document_config
    from .cli import get_run_command
    
    print_header("ONSHAPE EXPORT TOOL - SETUP WIZARD")
    
    # Step 1: API Credentials (only if not already configured)
    print_section("Step 1: API Credentials")
    existing_secrets = load_secrets(secrets_path)
    if existing_secrets:
        print(f"✓ Secrets already configured ({secrets_path})")
    else:
        secrets = prompt_secrets()
        save_secrets(secrets, secrets_path)
    
    # Step 2: Document Configuration
    print_section("Step 2: Document Configuration")
    did, wid = prompt_document_config()
    save_document_config(did, wid, config_path)
    
    print_header("SETUP COMPLETE!")
    print(f"\nSecrets: {secrets_path}")
    print(f"Config: {config_path}")
    print(f"\nYou can now run exports with: {get_run_command()}")


# --- Interactive Export Flow ---

def run_interactive_export(client: Any, output_dir: Path) -> Optional[Path]:
    """Full interactive export flow with document selection and options."""
    from .client import (
        list_documents, list_workspaces, list_versions,
        make_workspace_context, make_version_context
    )
    from .workflow import run_export_workflow
    
    print_header("INTERACTIVE EXPORT")
    
    # Step 1: Select document
    print_section("Step 1: Select Document")
    print("Fetching recent documents...")
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
    print(f"\n✓ Selected: {doc['name']}")
    
    # Step 2: Choose workspace or version
    print_section("Step 2: Select Workspace/Version")
    print("Fetching workspaces and versions...")
    workspaces = list_workspaces(client, did)
    versions = list_versions(client, did)
    
    # Build combined list
    options: List[Dict[str, Any]] = []
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
    
    is_version = choice['type'] == 'version'
    print(f"\n✓ Selected: [{choice['type'].upper()}] {choice['name']}")
    
    # Step 3: Export Options (toggleable)
    print_section("Step 3: Export Options")
    
    if is_version:
        print("Note: Clean options are not available for version exports (read-only)")
        clean_before = False
        clean_after = False
    else:
        export_options = {
            "Clean before export (delete existing DXFs/PDFs)": False,
            "Clean after export (remove generated files from document)": False,
        }
        
        final_options = interactive_toggles(export_options)
        clean_before = final_options.get("Clean before export (delete existing DXFs/PDFs)", False)
        clean_after = final_options.get("Clean after export (remove generated files from document)", False)
    
    # Step 4: Create context and run
    print_section("Step 4: Running Export")
    
    if is_version:
        ctx = make_version_context(did, choice['id'])
        print(f"Exporting from version: {choice['name']}")
    else:
        ctx = make_workspace_context(did, choice['id'])
        print(f"Exporting from workspace: {choice['name']}")
    
    return run_export_workflow(client, ctx, output_dir,
                               clean_before=clean_before, clean_after=clean_after)


# Type alias for client (avoid circular import)
from typing import Any
