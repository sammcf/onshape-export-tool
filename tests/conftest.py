# Test configuration and shared fixtures
import pytest
from unittest.mock import Mock

@pytest.fixture
def mock_client():
    """Create a mock OnshapeClient."""
    return Mock()


@pytest.fixture
def sample_ctx():
    """Create a sample workspace DocContext for testing."""
    return {'did': 'test_doc', 'wvm_type': 'w', 'wvm_id': 'test_ws'}


@pytest.fixture
def version_ctx():
    """Create a sample version DocContext for testing."""
    return {'did': 'test_doc', 'wvm_type': 'v', 'wvm_id': 'test_version'}
