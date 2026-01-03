FeatureScript 2433;
import(path : "onshape/std/common.fs", version : "2433.0");
import(path : "onshape/std/math.fs", version : "2433.0");
import(path : "onshape/std/evaluate.fs", version : "2433.0");

/**
 * Plate Thickness — Computed Part Property
 * 
 * Automatically calculates the thickness of plate parts for use in BOMs,
 * part naming, and downstream manufacturing processes.
 * 
 * A "plate" is defined as a body where:
 *   - The two largest planar faces account for >50% of total planar area
 *   - Those faces have opposing normals (parallel but facing opposite directions)
 * 
 * Returns 0mm for non-plate parts.
 */

const MIN_DOMINANCE_RATIO = 0.5;   // Top two faces must be >50% of planar area
const PARALLEL_THRESHOLD = -0.95; // Dot product threshold for opposing normals

/**
 * Get all planar faces with their areas, sorted descending by area.
 */
function getFacesByArea(context is Context, faces is Query) returns array
{
    var result = [];
    for (var face in evaluateQuery(context, faces))
    {
        try silent
        {
            const area = evArea(context, { "entities" : face });
            if (area > TOLERANCE.zeroLength * meter^2)
            {
                result = append(result, { "face" : face, "area" : area });
            }
        }
    }
    return sort(result, function(a, b) { return b.area - a.area; });
}

/**
 * Calculate perpendicular distance between two faces.
 */
function getFaceDistance(context is Context, face1 is Query, face2 is Query) returns ValueWithUnits
{
    try silent
    {
        return evDistance(context, {
            "side0" : face1,
            "side1" : face2,
            "extendSide0" : true,
            "extendSide1" : true
        }).distance;
    }
    return 0 * meter;
}

/**
 * Validate if a part is a plate and calculate its thickness.
 * Returns { isPlate, thickness }.
 */
function getPlateThickness(context is Context, part is Query) returns map
{
    const bodies = qBodyType(part, BodyType.SOLID);
    if (size(evaluateQuery(context, bodies)) == 0)
    {
        return { "isPlate" : false };
    }
    
    const planarFaces = qGeometry(qOwnedByBody(bodies, EntityType.FACE), GeometryType.PLANE);
    const facesByArea = getFacesByArea(context, planarFaces);
    
    if (size(facesByArea) < 2)
    {
        return { "isPlate" : false };
    }
    
    // Check face dominance
    var totalArea = 0 * meter^2;
    for (var fa in facesByArea)
    {
        totalArea += fa.area;
    }
    
    if ((facesByArea[0].area + facesByArea[1].area) / totalArea < MIN_DOMINANCE_RATIO)
    {
        return { "isPlate" : false };
    }
    
    // Check opposing normals
    const plane1 = evPlane(context, { "face" : facesByArea[0].face });
    const plane2 = evPlane(context, { "face" : facesByArea[1].face });
    
    if (dot(plane1.normal, plane2.normal) > PARALLEL_THRESHOLD)
    {
        return { "isPlate" : false };
    }
    
    // Calculate thickness
    const thickness = getFaceDistance(context, facesByArea[0].face, facesByArea[1].face);
    
    if (thickness <= 0 * meter)
    {
        return { "isPlate" : false };
    }
    
    return { "isPlate" : true, "thickness" : thickness };
}

/**
 * Computed property: Plate thickness in mm.
 * Returns 0 for non-plate parts.
 */
annotation { "Property Function Name" : "Thickness" }
export const plateThickness = defineComputedPartProperty(function(context is Context, part is Query, definition is map) returns ValueWithUnits
    {
        const result = getPlateThickness(context, part);
        
        if (result.isPlate != true)
        {
            return 0 * millimeter;
        }
        
        return result.thickness;
    });
