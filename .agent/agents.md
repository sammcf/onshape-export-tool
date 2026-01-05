# Onshape Export Tool — Agent Guidelines

## Project Overview

Onshape automation project for manufacturing artifact export. Two-part system:
- **FeatureScripts**: Prepare geometry and compute metadata (plate orientation, thickness)
- **Export CLI**: Python tool (`onshape_export_tool.py`) for API-driven batch export of DXFs and PDFs.

**Current State**:
- FeatureScripts are implemented.
- Python CLI is fully functional (`onshape_export_tool.py`).
  - Workflow: Unsuppress "Orient Plates" -> Create Temp Drawing -> Export DXF -> Delete Temp Drawing.
  - Supports exporting existing drawings to PDF.
  - Generates a timestamped ZIP file with all artifacts and a log file.
  - Automatically cleans up temporary elements from failed runs.

---

## Build/Test/Lint Commands

### FeatureScript
No formal build system. Testing requires manual verification in Onshape.

### Python CLI
```bash
# Install dependencies
pip install -r requirements.txt

# Run CLI (uses config file)
python onshape_export_tool.py --verbose

# CLI Args
# --out <dir> : Specify output directory (default: exports)
# --doc <id>  : Override document ID
# --work <id> : Override workspace ID
```

---

## FeatureScript Code Style Guidelines

### File Structure

```feature
FeatureScript 2433;
import(path : "onshape/std/common.fs", version : "2433.0");
import(path : "onshape/std/math.fs", version : "2433.0");
// ... other imports

// Constants first
const CONSTANT_NAME = 0.5;  // Inline comment explaining value

// Enums after constants
export enum EnumName
{
    annotation { "Name" : "UI Name" }
    ENUM_VALUE,
}

// Helper functions
function helperFunction(param is Type) returns ReturnType
{
    // Implementation
}

// Exported feature/property
annotation { "Feature Type Name" : "Feature Name" }
export const featureName = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Param Name", "Default" : DefaultValue }
        definition.param is Type;
    }
    {
        // Implementation
    });
```

### Naming Conventions

- **Constants**: `UPPER_SNAKE_CASE` with inline comments
  ```feature
  const MIN_DOMINANCE_RATIO = 0.5;   // Top two faces must be >50% of planar area
  ```

- **Functions**: `camelCase` with descriptive names
  ```feature
  function detectPlate(context is Context, body is Query) returns map
  ```

- **Enums**: `UPPER_SNAKE_CASE` for values
  ```feature
  export enum TargetAxis
  {
      X_AXIS,
      Y_AXIS,
      Z_AXIS
  }
  ```

- **Variables**: `camelCase`
  ```feature
  const planarFaces = qGeometry(...);
  var bodiesToDelete = [];
  ```

### Type Annotations

Always specify types for parameters and return values:
```feature
function calculateTransform(plane is Plane, axis is Vector) returns Transform
{
    // Implementation
}
```

Common types: `Context`, `Id`, `Query`, `map`, `array`, `ValueWithUnits`, `Transform`, `Vector`, `Plane`, `boolean`, `number`

### Comments

- **Functions**: JSDoc-style multiline comments
  ```feature
  /**
   * Calculate transform to align plate normal with target axis.
   * @param facePlane - The plane of the plate face
   * @param targetAxis - The target axis to align with
   * @return Transform to apply
   */
  ```

- **Inline**: Brief explanatory comments
  ```feature
  // Try qLargest first — returns all faces with equal max area
  const largestFaces = qLargest(planarFaces);
  ```

### Error Handling

Use `try silent` for operations that may fail:
```feature
try silent
{
    const area = evArea(context, { "entities" : face });
}
// Handle failure case separately
```

### Return Values

Functions returning complex data should use maps with string keys:
```feature
return { "isPlate" : true, "thickness" : value };
return { "isPlate" : false };
```

Access with dot notation: `result.isPlate`, `result.thickness`

### Annotations

For UI-facing features and properties:
```feature
// Features
annotation { "Feature Type Name" : "Feature Name" }
export const featureName = defineFeature(...)

// Parameters inside features
annotation { "Name" : "Target Axis", "Default" : TargetAxis.Z_AXIS }
definition.targetAxis is TargetAxis;

// Enums with UI names
export enum TargetAxis
{
    annotation { "Name" : "X Axis" }
    X_AXIS,
}

// Computed properties
annotation { "Property Function Name" : "Thickness" }
export const plateThickness = defineComputedPartProperty(...)
```

### Query Patterns

Use standard Onshape query functions:
```feature
const planarFaces = qGeometry(qOwnedByBody(body, EntityType.FACE), GeometryType.PLANE);
const allBodies = qAllModifiableSolidBodies();
const largestFaces = qLargest(planarFaces);
```

Evaluate queries before iteration:
```feature
const bodyArray = evaluateQuery(context, allBodies);
for (var i = 0; i < size(bodyArray); i += 1)
{
    const body = bodyArray[i];
    // Process body
}
```

### User Feedback

Report user-friendly messages for feature actions:
```feature
if (transformCount > 0)
{
    const message = transformCount ~ " plate" ~ (transformCount == 1 ? "" : "s") ~ " oriented";
    reportFeatureInfo(context, id, message);
}
```

---

## Python Code Style Guidelines

The `onshape_export_tool.py` follows these patterns:

- **Type Hints**: Extensive use of `typing` (Dict, Any, List, Optional, cast).
- **Configuration**: JSON-based config file (not committed).
- **Logging**: Comprehensive logging to console and file.
- **Workflow**:
  1. **Pre-flight Cleanup**: Deletes `TEMP_` elements.
  2. **Discovery**: Lists Part Studios and Drawings (including Application elements).
  3. **Feature Logic**: Regex selection of highest-indexed "Orient Plates for Export" feature.
  4. **Drawing Creation**: Creates empty ISO/mm drawing, adds 1:1 Top view, waits for microversion update.
  5. **Export**: Translates to DXF (plates) or PDF (drawings), stores in document.
  6. **Cleanup**: Deletes temporary drawings immediately.
  7. **Packaging**: Downloads all exports to ZIP.

Example structure:
```python
import requests
import logging

def export_dxf(document_id: str, workspace_id: str) -> str:
    """Export plates as DXF from specified document."""
    try:
        response = requests.post(url, auth=(access_key, secret_key))
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        logging.error(f"Export failed: {e}")
        raise
```

---

## Key Practices

### Prefer Onshape Built-in Functions

Use standard library functions over manual implementations:
- `qLargest()` instead of manual sorting for largest faces
- `evArea()`, `evPlane()`, `evDistance()` for geometry evaluation
- `opTransform()`, `opDeleteBodies()` for operations

### Validate Geometry Before Operations

Always check query results exist before processing:
```feature
const bodies = qBodyType(part, BodyType.SOLID);
if (size(evaluateQuery(context, bodies)) == 0)
{
    return { "isPlate" : false };
}
```

### Handle Secrets Securely

- API keys and document IDs stored in a single `config` file (JSON format)
- **CRITICAL**: The `config` file must not be committed to version control
- Bruno environments use variables: `{{accessKey}}`, `{{secretKey}}`
- Never hardcode credentials in FeatureScript

### Report User-Friendly Messages

Use singular/plural logic and clear descriptions:
```feature
message = deleteCount ~ " non-plate" ~ (deleteCount == 1 ? "" : "s") ~ " removed";
```

### Keep Functions Focused

Each function should do one thing well. Break complex logic into helper functions:
- `detectPlate()` - identifies if body is a plate
- `validatePlateFaces()` - validates plate criteria
- `calculateAlignmentTransform()` - computes rotation
- `getAxisVector()` - converts enum to vector

---

## Bruno API Collection Guidelines

When adding or modifying API requests:

- Use environment variables for dynamic values: `{{baseUrl}}`, `{{accessKey}}`, `{{secretKey}}`
- Follow REST naming: documents by ID (`/documents/d/:did/w/:wid`)
- **Note**: Always use workspaces (`/w/`) instead of versions (`/v/`) for API endpoints.
- Use `auth: basic` for Onshape API authentication
- Test endpoints manually before automating in Python CLI
- Document any required parameters in `meta { name : "..." }`

---

## Testing Workflow

### FeatureScript Testing
1. Import `.fs` file into Onshape Part Studio
2. Create test geometry (plates, non-plates)
3. Run feature and verify transformations
4. Check FeatureScript console for errors/warnings
5. Verify computed properties in part properties dialog

### API Testing (Bruno)
1. Use Bruno collection under `Onshape REST API/`
2. Select appropriate environment (Base Env, Testing)
3. Run requests and verify responses
4. Check for correct status codes and data structures

---

## Notes

- FeatureScript version: 2433
- Onshape API version: v12 (https://cad.onshape.com/api/v12)
- Computed properties are NOT accessible via REST API (UI only)
- The `orient_plates.fs` function intentionally deletes non-plate bodies
- Thickness computation is for BOMs and naming rules, not API export

---

## Known Issues & Limitations

- **Auto Centermarks**: ~~Disabling "Auto Centermarks" on temporary drawing views via the API remains partially unresolved.~~
  - **RESOLVED**: The script now uses `showCentermarks: False` and `showCenterlines: False` in view creation (per the Onshape Drawing JSON schema), and `includeFormedCentermarks: False` in the translation payload.

## Future Development & Stretch Goals

- [DONE] **Export Rules for Filenames**: Ensure that all exported manufacturing artifacts are saved with the correct file names according to export rules.
- [DONE] **Handle Sheetmetal Parts Correctly**: Ensure that sheetmetal part flat patterns are exported correctly. This will probably require a change to the overall workflow: finding flat patterns and exporting them prior to unsuppressing the orient feature.
- [DONE] **Handle Part Thickness for Export Rules**: Ensure that part thickness is used correctly in export rules. Given this is a computed property, we may need to radically change our strategy for handling it.
- [DONE] **Packaged Script**: Use PyInstaller to package the script into a single executable file that can be run on any system with no other dependencies required.
- [DONE] **Test Suite**: pytest-based test suite with pure function tests and mocked API tests (34 tests).
- [DONE] **Functional Refactor**: `DocContext` for workspace/version abstraction, `Secrets` for credentials, `pipeline()` for function composition, `WorkflowState` for immutable step flow.
- [DONE] **Clean Run Option**: `--clean-before` deletes existing DXF/PDF blobs before export; `--clean-after` deletes them after packaging. Both flags are workspace-only (ignored for version exports).
- [DONE] **Workspace vs. Version Parameterization**: `--version-id <id>` exports from an immutable document version instead of the active workspace.
- [DONE] **Interactive Secrets Management**: `--setup` runs wizard; secrets stored in `.secrets`, document config in `config`. Prompts on first run.
- [DONE] **Interactive Workflow**: `--interactive` lists recent documents, lets user select document → workspace/version → runs export.
- [SPECULATIVE] **Part Filtering and Orientation**: Investigate if it's possible to bring the plate part filtering and orientation logic over to the API side, or if it should stay as a FeatureScript in Onshape.

## Important Takeaways

- The Onshape REST API is incredibly powerful. Having a local spec and testing setup with Bruno suggests a lot of future possibilities.
- I erroneously assumed that computed properties would not be available over the wire but *they are* - you just have to query the appropriate metadata endpoint with the right parameters.
- Lots of refactoring and tidying up opportunities. In fact, a rewrite in go-lang probably wouldn't go astray - this has given me a strong understanding of the data flow and processes. Maybe using the [Charm framework?](https://charm.land/)