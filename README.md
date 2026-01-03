# Onshape Manufacturing Export Tool

A Python CLI tool that automates the export of manufacturing artifacts from Onshape documents.

## Features

- **DXF Export**: Automatically exports oriented plate parts as DXFs (1:1 scale, no centermarks)
- **PDF Export**: Exports existing drawings as PDFs
- **Automatic Cleanup**: Manages temporary drawings and cleans up after itself
- **ZIP Packaging**: Bundles all exports with an operation log

## Requirements

- Python 3.8+
- Onshape API credentials (access key and secret key)

## Installation

1. Clone this repository
2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

Create a `config` file in the project directory with your Onshape credentials:

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

1. Go to [Onshape User Profile > Developer > API Keys](https://cad.onshape.com/user/developer)
2. Create an API key with appropriate permissions
3. Copy the access key and secret key to your config file

### Finding Document and Workspace IDs

From any Onshape document URL:
```
https://cad.onshape.com/documents/d/{documentId}/w/{workspaceId}/e/{elementId}
```

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

1. **Cleanup**: Removes any leftover temporary elements from previous runs
2. **Discovery**: Finds all Part Studios and Drawings in the document
3. **Part Studio Export**: For each Part Studio with an "Orient Plates for Export" feature:
   - Unsuppresses the orient feature
   - Creates a temporary drawing for each part
   - Adds a 1:1 top view
   - Exports to DXF
   - Deletes the temporary drawing
   - Re-suppresses the orient feature
4. **Drawing Export**: Exports each existing drawing as PDF
5. **Packaging**: Downloads all exported files and bundles them into a ZIP

## FeatureScripts

This tool works with two companion FeatureScripts that should be added to your Onshape document:

- **Orient Plates for Export**: Orients plate bodies so their largest face is aligned with the XY plane
- **Plate Properties**: A convenience feature that provides a computed property for plate thickness for parts

## Output

The tool creates a timestamped ZIP file containing:
- All exported DXF files (one per plate part)
- All exported PDF files (one per drawing)
- `export_operation.log` — detailed log of the export operation

## License

MIT
