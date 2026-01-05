"""Pure function tests. No mocking needed."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from onshape_export_tool import (
    categorize_parts, 
    find_orient_feature, 
    format_thickness_prefix,
    doc_path,
    is_mutable,
    make_context
)


class TestCategorizeParts:
    def test_separates_flat_patterns(self):
        """Sheet metal flat patterns go to flats; their parent parts are filtered out."""
        parts = [
            {'partId': '1', 'isFlattenedBody': True, 'unflattenedPartId': '2', 'name': 'Flat 1'},
            {'partId': '2', 'isFlattenedBody': False, 'name': 'Sheet Metal'},
            {'partId': '3', 'isFlattenedBody': False, 'name': 'Regular Part'},
        ]
        flats, regular = categorize_parts(parts)
        
        assert len(flats) == 1
        assert flats[0]['partId'] == '1'
        assert len(regular) == 1
        assert regular[0]['partId'] == '3'
    
    def test_empty_input(self):
        flats, regular = categorize_parts([])
        assert flats == []
        assert regular == []
    
    def test_only_regular_parts(self):
        parts = [{'partId': '1', 'name': 'Part A'}, {'partId': '2', 'name': 'Part B'}]
        flats, regular = categorize_parts(parts)
        assert len(flats) == 0
        assert len(regular) == 2


class TestFindOrientFeature:
    def test_finds_single_feature(self):
        features = [
            {'name': 'Extrude 1', 'featureId': 'f1'},
            {'name': 'Orient Plates for Export', 'featureId': 'f2'},
            {'name': 'Fillet 1', 'featureId': 'f3'},
        ]
        result = find_orient_feature(features)
        assert result['featureId'] == 'f2'
    
    def test_finds_highest_indexed(self):
        """Multiple orient features: highest index wins (e.g. 'Orient Plates for Export 2')."""
        features = [
            {'name': 'Orient Plates for Export', 'featureId': 'f1'},
            {'name': 'Orient Plates for Export 2', 'featureId': 'f2'},
            {'name': 'Orient Plates for Export 1', 'featureId': 'f3'},
        ]
        result = find_orient_feature(features)
        assert result['featureId'] == 'f2'
    
    def test_returns_none_when_not_found(self):
        features = [{'name': 'Extrude 1', 'featureId': 'f1'}]
        assert find_orient_feature(features) is None
    
    def test_empty_features(self):
        assert find_orient_feature([]) is None


class TestFormatThicknessPrefix:
    @pytest.mark.parametrize("value,expected", [
        (3.0, "3mm"), (1.5, "1.5mm"), (2.0, "2mm"), (0.5, "0.5mm"),
        (None, ""), (0, ""), (-1.0, ""),
    ])
    def test_formatting(self, value, expected):
        assert format_thickness_prefix(value) == expected


class TestDocPath:
    def test_workspace_path(self):
        ctx = {'did': 'doc123', 'wvm_type': 'w', 'wvm_id': 'ws456'}
        assert doc_path(ctx) == "/d/doc123/w/ws456"
    
    def test_version_path(self):
        ctx = {'did': 'doc123', 'wvm_type': 'v', 'wvm_id': 'ver789'}
        assert doc_path(ctx) == "/d/doc123/v/ver789"
    
    def test_with_suffix(self):
        ctx = {'did': 'doc123', 'wvm_type': 'w', 'wvm_id': 'ws456'}
        assert doc_path(ctx, "/elements") == "/d/doc123/w/ws456/elements"


class TestIsMutable:
    def test_workspace_is_mutable(self):
        assert is_mutable({'did': 'doc', 'wvm_type': 'w', 'wvm_id': 'ws'}) is True
    
    def test_version_is_not_mutable(self):
        assert is_mutable({'did': 'doc', 'wvm_type': 'v', 'wvm_id': 'ver'}) is False
    
    def test_microversion_is_not_mutable(self):
        assert is_mutable({'did': 'doc', 'wvm_type': 'm', 'wvm_id': 'mv'}) is False


class TestMakeContext:
    def test_creates_workspace_context(self):
        ctx = make_context('doc123', 'ws456')
        assert ctx['did'] == 'doc123'
        assert ctx['wvm_type'] == 'w'
        assert ctx['wvm_id'] == 'ws456'


class TestMakeVersionContext:
    def test_creates_version_context(self):
        from onshape_export_tool import make_version_context
        ctx = make_version_context('doc123', 'ver789')
        assert ctx['did'] == 'doc123'
        assert ctx['wvm_type'] == 'v'
        assert ctx['wvm_id'] == 'ver789'
    
    def test_version_context_is_not_mutable(self):
        from onshape_export_tool import make_version_context
        ctx = make_version_context('doc', 'ver')
        assert is_mutable(ctx) is False


class TestPipeline:
    def test_composes_left_to_right(self):
        from onshape_export_tool import pipeline
        add_one = lambda x: x + 1
        double = lambda x: x * 2
        assert pipeline(add_one, double)(1) == 4  # (1+1)*2
    
    def test_single_function(self):
        from onshape_export_tool import pipeline
        assert pipeline(lambda x: x + 1)(5) == 6
    
    def test_empty_pipeline(self):
        from onshape_export_tool import pipeline
        assert pipeline()(42) == 42
