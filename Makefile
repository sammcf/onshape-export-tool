# Onshape Export Tool - Build Targets
# 
# Usage:
#   make build     - Build standalone executable
#   make clean     - Remove build artifacts
#   make test-run  - Quick syntax check

.PHONY: build clean test-run

# Executable name
NAME = onshape-export

build:
	@echo "Building standalone executable..."
	pyinstaller --onefile --name $(NAME) --console onshape_export_tool.py
	@echo ""
	@echo "Build complete: dist/$(NAME)"
	@echo "Copy the executable and run it. A config template will be created on first run."

clean:
	@echo "Cleaning build artifacts..."
	rm -rf build dist *.spec __pycache__ *.pyc
	@echo "Clean complete."

test-run:
	@python3 -m py_compile onshape_export_tool.py && echo "Syntax OK"
