"""API operations tests. Uses mocked OnshapeClient."""
import pytest
from unittest.mock import Mock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from onshape_export_tool import (
    list_elements,
    get_features,
    list_parts,
    delete_element,
    cleanup_exports,
)


class TestListElements:
    def test_returns_list_on_list_response(self, mock_client, sample_ctx):
        mock_client.request.return_value = [
            {'id': '1', 'name': 'PS1', 'elementType': 'PARTSTUDIO'},
            {'id': '2', 'name': 'Drawing', 'elementType': 'DRAWING'},
        ]
        
        result = list_elements(mock_client, sample_ctx)
        assert len(result) == 2
        assert result[0]['name'] == 'PS1'
    
    def test_extracts_elements_from_dict(self, mock_client, sample_ctx):
        """API sometimes wraps response in {'elements': [...]}."""
        mock_client.request.return_value = {'elements': [{'id': '1', 'name': 'PS1'}]}
        
        result = list_elements(mock_client, sample_ctx)
        assert len(result) == 1


class TestGetFeatures:
    def test_returns_features_list(self, mock_client, sample_ctx):
        mock_client.request.return_value = {
            'features': [
                {'name': 'Extrude 1', 'featureId': 'f1'},
                {'name': 'Fillet 1', 'featureId': 'f2'},
            ]
        }
        
        result = get_features(mock_client, sample_ctx, 'eid123')
        assert len(result) == 2


class TestListParts:
    def test_basic_call(self, mock_client, sample_ctx):
        mock_client.request.return_value = [{'partId': 'p1', 'name': 'Part 1'}]
        
        result = list_parts(mock_client, sample_ctx, 'eid123')
        assert len(result) == 1
        assert 'includeFlatParts' not in mock_client.request.call_args[1].get('params', {})
    
    def test_include_flat_parts(self, mock_client, sample_ctx):
        """includeFlatParts=true needed for sheet metal flat pattern discovery."""
        mock_client.request.return_value = []
        
        list_parts(mock_client, sample_ctx, 'eid123', include_flat_parts=True)
        assert mock_client.request.call_args[1]['params']['includeFlatParts'] == 'true'


class TestDeleteElement:
    def test_delete_makes_correct_call(self, mock_client, sample_ctx):
        mock_client.request.return_value = None
        
        delete_element(mock_client, sample_ctx, 'elem123')
        
        call_args = mock_client.request.call_args
        assert call_args[0][0] == 'DELETE'
        assert 'elem123' in call_args[0][1]


class TestCleanupExports:
    def test_skips_immutable_context(self, mock_client, version_ctx):
        """Cannot delete from versions/microversions."""
        result = cleanup_exports(mock_client, version_ctx)
        assert result == 0
        mock_client.request.assert_not_called()
    
    def test_finds_and_deletes_blobs(self, mock_client, sample_ctx):
        """Finds DXF/PDF blobs by extension and deletes them."""
        mock_client.request.side_effect = [
            [
                {'id': '1', 'name': 'export.dxf', 'elementType': 'BLOB'},
                {'id': '2', 'name': 'drawing.pdf', 'elementType': 'BLOB'},
                {'id': '3', 'name': 'Part Studio', 'elementType': 'PARTSTUDIO'},
            ],
            None, None,  # delete responses
        ]
        
        result = cleanup_exports(mock_client, sample_ctx)
        assert result == 2
        assert mock_client.request.call_count == 3  # 1 GET + 2 DELETE
