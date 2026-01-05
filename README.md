# Onshape Manufacturing Export Tool

This tool exports DXF files from parts and PDF files from drawings in Onshape.

## Instructions for Users

### First Time Setup

1. Download the tool for your operating system.
2. Open a terminal and run the tool with the setup flag:
   ./onshape_export_tool --setup
3. Enter your Onshape API keys when prompted.
4. Enter an encryption password to protect your keys on disk.
5. Enter the Document ID and Workspace ID of your project.

### Running Exports

Run the tool without flags to export the configured document:
./onshape_export_tool

Use the interactive flag to browse and select a different document:
./onshape_export_tool --interactive

### Flags

- --setup: Runs the guided setup process.
- --interactive: Browse and select documents to export.
- --out [directory]: Sets the output folder name.
- --verbose: Shows detailed logs of all operations.
- --clean-before: Deletes old DXF and PDF files from the document before exporting.
- --clean-after: Deletes the DXF and PDF files from the document after the ZIP is created.
- --version-id [id]: Exports from a specific version instead of a workspace.

### Password Management

To change your encryption password, delete the .secrets file and run the setup process again.

## Instructions for Building

### Requirements

- Python 3.10 or newer
- Pip

### Running from Source

1. Clone the repository.
2. Install dependencies:
   pip install -r requirements.txt
3. Run the script:
   python onshape_export_tool.py

### Creating the Executable

Run the build script to generate a single file executable for your system:
./build.sh

The executable is saved in the dist folder with the version and architecture in the name.

### Running Tests

Run the test suite using pytest:
python -m pytest tests/
