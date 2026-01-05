# Onshape Manufacturing Export Tool

A CLI tool that automates the export of manufacturing artifacts (DXFs and PDFs) from Onshape documents.

## Features

- **Sheet Metal Flat Patterns**: Automatically detects and exports sheet metal flat patterns directly
- **Plate DXF Export**: Exports oriented plate parts as DXFs (1:1 scale, no bend tangent lines)
- **PDF Export**: Exports existing drawings as PDFs
- **Thickness in Filenames**: Prepends part thickness to DXF filenames (e.g., `3mm_PART_NAME.dxf`)
- **Export Rules Support**: Uses Onshape export rules for filenames when configured
- **Filename Collision Handling**: Detects duplicate filenames and reports them at end of run
- **Standalone Executable**: Can be packaged as a single executable with PyInstaller
- **Auto-Config Template**: Creates a template config file on first run with instructions

## Quick Start

### Option 1: Standalone Executable

1. Download or build the executable (see [Building](#building))
2. Run it once — a `config` template will be created
3. Edit the `config` file with your credentials
4. Run again to export

### Option 2: Run from Source

```bash
# Clone and setup
git clone https://github.com/sammcf/onshape-export-tool.git
cd onshape-export-tool
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run
python onshape_export_tool.py
```

## Configuration

Create a `config` file (or run once to generate a template):

```json
{
    "accessKey": "YOUR_ACCESS_KEY",
    "secretKey": "YOUR_SECRET_KEY",
    "documentId": "YOUR_DOCUMENT_ID",
    "workspaceId": "YOUR_WORKSPACE_ID"
}
```

> **Never commit the `config` file** — it contains your API secrets.

### Getting Your Credentials

1. Go to [Onshape My Account > Developer > API Keys](https://cad.onshape.com/user/developer)
2. Create an API key with appropriate permissions
3. Copy the access key and secret key to your config

### Finding Document and Workspace IDs

From any Onshape document URL:
```
https://cad.onshape.com/documents/d/{documentId}/w/{workspaceId}/e/{elementId}
```

> **Note**: An "element" of an Onshape document is a tab.

## Usage

```bash
# Basic usage
python onshape_export_tool.py

# With verbose logging
python onshape_export_tool.py --verbose

# Specify output directory
python onshape_export_tool.py --out ./my-exports
```

## How It Works

1. **Cleanup**: Removes any leftover temporary elements from previous runs to ensure a consistent output state.
2. **Discovery**: Finds all Part Studios and Drawings in the document
3. **Sheet Metal Export**: Exports flat patterns from Part Studios directly (no orientation needed)
4. **Plate Export**: For non-sheet-metal parts in Part Studios which contain an "Orient Plates for Export" feature:
   - Unsuppresses the orient feature
   - Creates temporary drawings and exports to DXF
   - Re-suppresses the orient feature
5. **Drawing Export**: Exports each previously existing drawing as PDF
6. **Packaging**: Bundles all exports into a timestamped ZIP with operation log and stores them in the ../exports folder

## Building

Build a standalone executable with PyInstaller:

```bash
source venv/bin/activate
pip install pyinstaller
pyinstaller --onefile --name onshape-export onshape_export_tool.py
```

The executable will be in `dist/onshape-export`. Copy it anywhere and run.

> **Note**: Builds are platform-specific. Build on Windows for Windows, Linux for Linux, etc.

## FeatureScripts

This tool works with companion FeatureScripts that should be added to your Onshape document:

- **Orient Plates for Export**: Orients plate bodies so their largest face aligns with XY plane
- **Plate Properties**: Computed property for plate thickness (used in export filenames)

## Output

The tool creates a timestamped ZIP file containing:
- DXF files with thickness prefix (e.g., `3mm_Part_Name.dxf`)
- PDF files for drawings
- `export_operation.log` — detailed operation log

## License

MIT
