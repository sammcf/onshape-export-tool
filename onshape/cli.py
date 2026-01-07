"""CLI entry point and argument parsing.

Handles command-line interface and dispatches to appropriate workflows.
"""
import argparse
import logging
import sys
from pathlib import Path

from .client import OnshapeClient, make_workspace_context, make_version_context
from .secrets import get_or_prompt_secrets, load_secrets
from .ui import print_header, interactive_menu, run_setup_wizard, run_interactive_export
from .workflow import run_export_workflow


LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


def get_run_command() -> str:
    """Returns appropriate CLI command for frozen exe vs script mode."""
    if getattr(sys, 'frozen', False):
        return "./onshape_export_tool"
    return "python onshape_export_tool.py"


def run_main_menu(secrets_path: Path, config_path: Path, output_path: Path) -> None:
    """Main interactive menu loop."""
    print_header("ONSHAPE EXPORT TOOL")
    
    # Check if secrets exist - run setup if not
    existing_secrets = load_secrets(secrets_path)
    if not existing_secrets:
        print("\n⚠ No API credentials found. Running first-time setup...")
        run_setup_wizard(secrets_path, config_path)
        return
    
    # Main menu choices
    menu_options = [
        "Export a document",
        "Run setup wizard (reconfigure credentials/document)"
    ]
    
    choice = interactive_menu(menu_options, "What would you like to do?")
    
    if choice is None:
        print("Goodbye!")
        return
    
    if choice == 1:  # Setup wizard
        run_setup_wizard(secrets_path, config_path)
        return
    
    # Export document flow (choice == 0)
    secrets = get_or_prompt_secrets(secrets_path)
    client = OnshapeClient(secrets['access_key'], secrets['secret_key'])
    
    run_interactive_export(client, output_path)


def main():
    """Parse arguments, load config, and run the export workflow."""
    parser = argparse.ArgumentParser(
        description="Onshape Manufacturing Export Tool",
        epilog="Run without arguments for interactive mode."
    )
    parser.add_argument("--out", default="exports", help="Output directory")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    
    # Export options (can be set interactively or via flags)
    parser.add_argument("--clean-before", action="store_true",
                        help="Delete existing DXFs/PDFs before export")
    parser.add_argument("--clean-after", action="store_true",
                        help="Delete DXFs/PDFs from document after packaging")
    
    # Non-interactive mode flags
    parser.add_argument("--doc-id", help="Document ID for non-interactive export")
    parser.add_argument("--workspace-id", help="Workspace ID for non-interactive export")
    parser.add_argument("--version-id", help="Version ID (use instead of workspace for read-only)")
    
    parser.add_argument("--setup", action="store_true", help="Run setup wizard directly")
    
    args = parser.parse_args()

    # Determine base directory (works for both script and packaged exe)
    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent.parent
    
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format=LOG_FORMAT)

    secrets_path = base_dir / ".secrets"
    config_path = base_dir / "config"
    output_path = base_dir / args.out
    
    # Handle explicit --setup
    if args.setup:
        run_setup_wizard(secrets_path, config_path)
        return
    
    # Non-interactive mode: flags specify document
    if args.doc_id:
        if not args.workspace_id and not args.version_id:
            print("Error: --doc-id requires --workspace-id or --version-id")
            return
        
        secrets = get_or_prompt_secrets(secrets_path)
        client = OnshapeClient(secrets['access_key'], secrets['secret_key'])
        
        if args.version_id:
            ctx = make_version_context(args.doc_id, args.version_id)
            logging.info(f"Exporting from version: {args.version_id}")
            if args.clean_before or args.clean_after:
                logging.warning("--clean-before/--clean-after ignored: cannot modify immutable version")
                args.clean_before = args.clean_after = False
        else:
            ctx = make_workspace_context(args.doc_id, args.workspace_id)
        
        run_export_workflow(client, ctx, output_path,
                           clean_before=args.clean_before,
                           clean_after=args.clean_after)
        return
    
    # Default: interactive mode
    run_main_menu(secrets_path, config_path, output_path)


if __name__ == "__main__":
    main()
