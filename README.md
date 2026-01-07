# Onshape Manufacturing Export Tool

This tool exports DXF files from parts and PDF files from drawings in Onshape.

## Instructions for Users

### First Time Setup

1. Download the tool for your operating system.
2. Open a terminal and run the tool: ```./onshape_export_tool```
3. On first run, the setup wizard will automatically start.
4. Enter your Onshape API keys when prompted.
5. Enter an encryption password to protect your keys on disk.
6. Enter the Document ID and Workspace ID of your project.

### Running Exports

Run the tool without flags to see the main menu: ```./onshape_export_tool```

From the menu you can:
- Export a document (with document and workspace/version selection)
- Run the setup wizard to reconfigure credentials

### Flags

- ```--setup```: Runs the guided setup process directly.
- ```--out [directory]```: Sets the output folder name.
- ```--verbose```: Shows detailed logs of all operations.
- ```--clean-before```: Deletes old DXF and PDF files from the document before exporting.
- ```--clean-after```: Deletes the DXF and PDF files from the document after the ZIP is created.

For scripted or automated exports:
- ```--doc-id [id]```: Document ID for non-interactive export.
- ```--workspace-id [id]```: Workspace ID for non-interactive export.
- ```--version-id [id]```: Exports from a specific version instead of a workspace.

### Password Management

To change your encryption password, delete the ```.secrets``` file and run the setup process again.

## Instructions for Building

### Requirements

- Python 3.10 or newer
- Pip

### Running from Source

1. Clone the repository.
2. Install dependencies: ```pip install -r requirements.txt```
3. Run the script: ```python onshape_export_tool.py```

### Project Structure

The tool is organized as a Python package:
- onshape_export_tool.py: Entry point (thin wrapper)
- onshape/: Package containing the implementation
  - ```client.py```: API client and operations
  - ```secrets.py```: Credentials management
  - ```ui.py```: Terminal UI components
  - ```workflow.py```: Export business logic
  - ```cli.py```: Command-line interface

### Creating the Executable

Run the build script to generate a single file executable for your system: ```./build.sh```

The executable is saved in the dist folder.

### Running Tests

Run the test suite using pytest: ```python -m pytest tests/```
