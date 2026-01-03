FeatureScript 2433;
import(path : "onshape/std/common.fs", version : "2433.0");
import(path : "onshape/std/math.fs", version : "2433.0");
import(path : "onshape/std/transform.fs", version : "2433.0");
import(path : "onshape/std/evaluate.fs", version : "2433.0");
import(path : "onshape/std/coordSystem.fs", version : "2433.0");

/**
 * Orient Plates for Export
 * 
 * Prepares plate parts for DXF export by rotating them so their faces
 * are perpendicular to the target axis (default Z). Non-plate bodies
 * are deleted to simplify downstream export processing.
 * 
 * A "plate" is defined as a body where:
 *   - The two largest planar faces account for >50% of total planar area
 *   - Those faces have opposing normals (parallel but facing opposite directions)
 */

const MIN_DOMINANCE_RATIO = 0.5;   // Top two faces must be >50% of planar area
const PARALLEL_THRESHOLD = -0.95; // Dot product threshold for opposing normals

/**
 * Detect if a body is a plate. Returns { isPlate, facePlane } if valid.
 */
function detectPlate(context is Context, body is Query) returns map
{
    const planarFaces = qGeometry(qOwnedByBody(body, EntityType.FACE), GeometryType.PLANE);
    
    // Try qLargest first — returns all faces with equal max area
    const largestFaces = qLargest(planarFaces);
    const largestArray = evaluateQuery(context, largestFaces);
    
    if (size(largestArray) >= 2)
    {
        // Ideal case: two or more faces share the max area
        const area1 = evArea(context, { "entities" : largestArray[0] });
        const area2 = evArea(context, { "entities" : largestArray[1] });
        return validatePlateFaces(context, largestArray[0], largestArray[1], area1, area2, planarFaces);
    }
    
    // Fallback: manually find top 2 faces by area
    const allPlanarArray = evaluateQuery(context, planarFaces);
    if (size(allPlanarArray) < 2)
    {
        return { "isPlate" : false };
    }
    
    var facesWithAreas = [];
    for (var face in allPlanarArray)
    {
        try silent
        {
            const area = evArea(context, { "entities" : face });
            facesWithAreas = append(facesWithAreas, { "face" : face, "area" : area });
        }
    }
    
    if (size(facesWithAreas) < 2)
    {
        return { "isPlate" : false };
    }
    
    facesWithAreas = sort(facesWithAreas, function(a, b) { return b.area - a.area; });
    
    return validatePlateFaces(context, facesWithAreas[0].face, facesWithAreas[1].face,
                               facesWithAreas[0].area, facesWithAreas[1].area, planarFaces);
}

/**
 * Check if two candidate faces form a valid plate (dominance + opposing normals).
 */
function validatePlateFaces(context is Context, face1 is Query, face2 is Query,
                            area1 is ValueWithUnits, area2 is ValueWithUnits,
                            allPlanarFaces is Query) returns map
{
    // Calculate total planar area for dominance check
    var totalArea = 0 * meter^2;
    for (var face in evaluateQuery(context, allPlanarFaces))
    {
        try silent
        {
            totalArea += evArea(context, { "entities" : face });
        }
    }
    
    if ((area1 + area2) / totalArea < MIN_DOMINANCE_RATIO)
    {
        return { "isPlate" : false };
    }
    
    // Check that face normals are opposing (dot product near -1)
    const plane1 = evPlane(context, { "face" : face1 });
    const plane2 = evPlane(context, { "face" : face2 });
    
    if (dot(plane1.normal, plane2.normal) > PARALLEL_THRESHOLD)
    {
        return { "isPlate" : false };
    }
    
    return { "isPlate" : true, "facePlane" : plane1 };
}

/**
 * Calculate transform to align plate normal with target axis.
 */
function calculateAlignmentTransform(facePlane is Plane, targetAxis is Vector) returns Transform
{
    const normal = facePlane.normal;
    const dotProd = dot(normal, targetAxis);
    
    // Already aligned
    if (abs(dotProd) > 0.9999)
    {
        if (dotProd > 0)
        {
            return identityTransform();
        }
        // Opposite direction — rotate 180° around a perpendicular axis
        var perpAxis = abs(normal[0]) < 0.9 ? vector(1, 0, 0) : vector(0, 1, 0);
        perpAxis = normalize(cross(normal, perpAxis));
        return rotationAround(line(vector(0, 0, 0) * meter, perpAxis), 180 * degree);
    }
    
    // Build coordinate systems and compute transform between them
    const currentCSys = coordSystem(vector(0, 0, 0) * meter, facePlane.x, normal);
    
    var targetX = abs(targetAxis[0]) < 0.9 ? vector(1, 0, 0) : vector(0, 1, 0);
    targetX = normalize(cross(cross(targetAxis, targetX), targetAxis));
    const targetCSys = coordSystem(vector(0, 0, 0) * meter, targetX, targetAxis);
    
    return toWorld(targetCSys) * fromWorld(currentCSys);
}

export enum TargetAxis
{
    annotation { "Name" : "X Axis" }
    X_AXIS,
    annotation { "Name" : "Y Axis" }
    Y_AXIS,
    annotation { "Name" : "Z Axis" }
    Z_AXIS
}

function getAxisVector(axis is TargetAxis) returns Vector
{
    if (axis == TargetAxis.X_AXIS)
        return vector(1, 0, 0);
    else if (axis == TargetAxis.Y_AXIS)
        return vector(0, 1, 0);
    else
        return vector(0, 0, 1);
}

annotation { "Feature Type Name" : "Orient Plates for Export" }
export const orientPlatesForExport = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Target Axis", "Default" : TargetAxis.Z_AXIS }
        definition.targetAxis is TargetAxis;
    }
    {
        const targetVector = getAxisVector(definition.targetAxis);
        const allBodies = qAllModifiableSolidBodies();
        const bodyArray = evaluateQuery(context, allBodies);
        
        var transformCount = 0;
        var bodiesToDelete = [];
        
        for (var i = 0; i < size(bodyArray); i += 1)
        {
            const body = bodyArray[i];
            const plateResult = detectPlate(context, body);
            
            if (plateResult.isPlate != true)
            {
                bodiesToDelete = append(bodiesToDelete, body);
                continue;
            }
            
            const transform = calculateAlignmentTransform(plateResult.facePlane, targetVector);
            
            if (transform == identityTransform())
            {
                continue; // Already aligned
            }
            
            opTransform(context, id + ("transform" ~ i), {
                "bodies" : qUnion([body]),
                "transform" : transform
            });
            transformCount += 1;
        }
        
        // Delete non-plate bodies
        if (size(bodiesToDelete) > 0)
        {
            opDeleteBodies(context, id + "deleteNonPlates", {
                "entities" : qUnion(bodiesToDelete)
            });
        }
        
        // Report summary to user
        const deleteCount = size(bodiesToDelete);
        var message = "";
        if (deleteCount > 0)
        {
            message = deleteCount ~ " non-plate" ~ (deleteCount == 1 ? "" : "s") ~ " removed";
            if (transformCount > 0)
                message = message ~ ", ";
        }
        if (transformCount > 0)
        {
            message = message ~ transformCount ~ " plate" ~ (transformCount == 1 ? "" : "s") ~ " oriented";
        }
        else if (deleteCount == 0)
        {
            message = "No plates found to orient";
        }
        
        if (message != "")
            reportFeatureInfo(context, id, message);
    });
