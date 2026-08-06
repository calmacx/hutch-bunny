import pytest
from hutch_bunny.core.rquest_models.distribution import (
    DistributionQuery,
    DistributionQueryType,
    LocationScanType,
)


def test_distribution_query_type_enum() -> None:
    """Test the DistributionQueryType enum values and properties."""
    # Test valid enum values
    assert DistributionQueryType.DEMOGRAPHICS.value == "DEMOGRAPHICS"
    assert DistributionQueryType.GENERIC.value == "GENERIC"
    assert DistributionQueryType.ICD_MAIN.value == "ICD-MAIN"

    # Test file_name property for valid types
    assert DistributionQueryType.DEMOGRAPHICS.file_name == "demographics.distribution"
    assert DistributionQueryType.GENERIC.file_name == "code.distribution"

    # Test file_name property for invalid type
    with pytest.raises(
        ValueError,
        match="No file name mapping for query type: DistributionQueryType.ICD_MAIN",
    ):
        _ = DistributionQueryType.ICD_MAIN.file_name


def test_distribution_query_creation() -> None:
    """Test creating valid DistributionQuery instances."""
    # Test valid query creation
    query = DistributionQuery(
        owner="user1",
        code=DistributionQueryType.DEMOGRAPHICS,
        analysis="DISTRIBUTION",
        uuid="test-uuid",
        collection="test-collection",
    )

    assert query.owner == "user1"
    assert query.code == DistributionQueryType.DEMOGRAPHICS
    assert query.analysis == "DISTRIBUTION"
    assert query.uuid == "test-uuid"
    assert query.collection == "test-collection"


def test_distribution_query_validation() -> None:
    """Test validation of DistributionQuery fields."""
    # Test invalid code
    with pytest.raises(
        ValueError, match="'INVALID' is not a valid distribution query type"
    ):
        DistributionQuery(
            owner="user1",
            code="INVALID",  # type: ignore
            analysis="DISTRIBUTION",
            uuid="test-uuid",
            collection="test-collection",
        )

    # Test invalid analysis
    with pytest.raises(ValueError):
        DistributionQuery(
            owner="user1",
            code=DistributionQueryType.DEMOGRAPHICS,
            analysis="INVALID",  # type: ignore
            uuid="test-uuid",
            collection="test-collection",
        )


def test_distribution_query_required_fields() -> None:
    """Test that all required fields are enforced."""
    # Test missing required field
    with pytest.raises(ValueError):
        DistributionQuery(
            owner="user1",
            code=DistributionQueryType.DEMOGRAPHICS,
            analysis="DISTRIBUTION",
            collection="test-collection",
        )

    with pytest.raises(ValueError):
        DistributionQuery(
            owner="user1",
            code=DistributionQueryType.DEMOGRAPHICS,
            analysis="DISTRIBUTION",
            uuid="test-uuid",
        )


def test_location_scan_type_enum() -> None:
    """Test the LocationScanType enum values."""
    assert LocationScanType.SOURCE_VALUE.value == "SOURCE_VALUE"
    assert LocationScanType.CONCEPT_CODE.value == "CONCEPT_CODE"
    assert LocationScanType.LAT_LONG.value == "LAT_LONG"


@pytest.mark.parametrize(
    "scan_type",
    [
        LocationScanType.SOURCE_VALUE,
        LocationScanType.CONCEPT_CODE,
        LocationScanType.LAT_LONG,
    ],
)
def test_location_query_with_valid_scan_type(scan_type: LocationScanType) -> None:
    """Test creating a LOCATION query with each valid location_scan_type."""
    query = DistributionQuery(
        owner="user1",
        code=DistributionQueryType.LOCATION,
        analysis="DISTRIBUTION",
        uuid="test-uuid",
        collection="test-collection",
        location_scan_type=scan_type,
    )

    assert query.location_scan_type == scan_type


def test_location_query_missing_scan_type_raises() -> None:
    """Test that a LOCATION query without location_scan_type is rejected."""
    with pytest.raises(ValueError, match="'location_scan_type' is required"):
        DistributionQuery(
            owner="user1",
            code=DistributionQueryType.LOCATION,
            analysis="DISTRIBUTION",
            uuid="test-uuid",
            collection="test-collection",
        )


def test_location_query_invalid_scan_type_raises() -> None:
    """Test that an invalid location_scan_type value is rejected."""
    with pytest.raises(
        ValueError, match="'INVALID' is not a valid location scan type"
    ):
        DistributionQuery(
            owner="user1",
            code=DistributionQueryType.LOCATION,
            analysis="DISTRIBUTION",
            uuid="test-uuid",
            collection="test-collection",
            location_scan_type="INVALID",  # type: ignore
        )


def test_non_location_query_does_not_require_scan_type() -> None:
    """Test that non-LOCATION queries don't require location_scan_type."""
    query = DistributionQuery(
        owner="user1",
        code=DistributionQueryType.GENERIC,
        analysis="DISTRIBUTION",
        uuid="test-uuid",
        collection="test-collection",
    )

    assert query.location_scan_type is None
