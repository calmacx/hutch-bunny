import pytest
from unittest.mock import MagicMock, Mock

from hutch_bunny.core.solvers.availability_solver import ResultModifier
from hutch_bunny.core.solvers.distribution_solver import LocationDistributionQuerySolver
from hutch_bunny.core.rquest_models.distribution import (
    DistributionQuery,
    DistributionQueryType,
    LocationScanType,
)


def _make_query(scan_type: LocationScanType) -> DistributionQuery:
    return DistributionQuery(
        owner="user1",
        code=DistributionQueryType.LOCATION,
        analysis="DISTRIBUTION",
        uuid="test-uuid",
        collection="test_collection",
        location_scan_type=scan_type,
    )


@pytest.fixture
def mock_db_client() -> Mock:
    """Create a mock db client with a mock engine."""
    db_client = Mock()
    db_client.engine = MagicMock()
    return db_client


def _wire_fetchall(mock_db_client: Mock, rows: list[tuple]) -> Mock:
    """Wire db_client.engine.connect() so `con.execute(stmnt).fetchall()` returns rows."""
    con = MagicMock()
    con.execute.return_value.fetchall.return_value = rows
    mock_db_client.engine.connect.return_value.__enter__.return_value = con
    mock_db_client.engine.connect.return_value.__exit__.return_value = False
    return con


def test_solve_query_source_value(mock_db_client: Mock) -> None:
    """SOURCE_VALUE scan groups by location_source_value only."""
    query = _make_query(LocationScanType.SOURCE_VALUE)
    solver = LocationDistributionQuerySolver(mock_db_client, query)
    con = _wire_fetchall(mock_db_client, [(20, "S01013497"), (30, None)])

    tsv, count = solver.solve_query([])

    con.execute.assert_called_once()
    assert count == 2
    header, *lines = tsv.splitlines()
    assert header.split("\t") == solver.output_cols
    row = dict(zip(solver.output_cols, lines[0].split("\t")))
    assert row["CODE"] == "S01013497"
    assert row["COUNT"] == "20"
    assert row["CATEGORY"] == "Location"
    assert row["OMOP"] == ""
    # None source value falls back to empty string, not the literal "None"
    row2 = dict(zip(solver.output_cols, lines[1].split("\t")))
    assert row2["CODE"] == ""


def test_solve_query_concept_code(mock_db_client: Mock) -> None:
    """CONCEPT_CODE scan groups by country_concept_id and joins Concept for the name."""
    query = _make_query(LocationScanType.CONCEPT_CODE)
    solver = LocationDistributionQuerySolver(mock_db_client, query)
    con = _wire_fetchall(mock_db_client, [(40, 4330435, "United Kingdom")])

    tsv, count = solver.solve_query([])

    con.execute.assert_called_once()
    assert count == 1
    header, row_line = tsv.splitlines()
    row = dict(zip(solver.output_cols, row_line.split("\t")))
    assert row["CODE"] == "4330435"
    assert row["OMOP"] == "4330435"
    assert row["OMOP_DESCR"] == "United Kingdom"
    assert row["DESCRIPTION"] == "United Kingdom"
    assert row["COUNT"] == "40"
    assert row["CATEGORY"] == "Location"


def test_solve_query_lat_long(mock_db_client: Mock) -> None:
    """LAT_LONG scan groups by unique (latitude, longitude) pairs."""
    query = _make_query(LocationScanType.LAT_LONG)
    solver = LocationDistributionQuerySolver(mock_db_client, query)
    con = _wire_fetchall(mock_db_client, [(15, 55.9533, -3.1883)])

    tsv, count = solver.solve_query([])

    con.execute.assert_called_once()
    assert count == 1
    header, row_line = tsv.splitlines()
    row = dict(zip(solver.output_cols, row_line.split("\t")))
    assert row["CODE"] == "55.9533,-3.1883"
    assert row["COUNT"] == "15"
    assert row["CATEGORY"] == "Location"
    assert row["OMOP"] == ""


def test_solve_query_applies_results_modifier(mock_db_client: Mock) -> None:
    """Counts pass through apply_filters, e.g. low-number suppression to 0."""
    query = _make_query(LocationScanType.SOURCE_VALUE)
    solver = LocationDistributionQuerySolver(mock_db_client, query)
    _wire_fetchall(mock_db_client, [(8, "S01013497")])
    modifiers: list[ResultModifier] = [
        {"id": "Low Number Suppression", "threshold": 10}  # type: ignore
    ]

    tsv, count = solver.solve_query(modifiers)

    assert count == 1
    header, row_line = tsv.splitlines()
    row = dict(zip(solver.output_cols, row_line.split("\t")))
    assert row["COUNT"] == "0"


def test_solve_query_no_rows(mock_db_client: Mock) -> None:
    """An empty result set produces a header-only TSV and a zero row count."""
    query = _make_query(LocationScanType.LAT_LONG)
    solver = LocationDistributionQuerySolver(mock_db_client, query)
    _wire_fetchall(mock_db_client, [])

    tsv, count = solver.solve_query([])

    assert count == 0
    assert tsv.splitlines() == ["\t".join(solver.output_cols)]


def test_solve_query_requires_location_scan_type(mock_db_client: Mock) -> None:
    """Defensive guard: solve_query rejects a missing location_scan_type even
    though DistributionQuery validation should already prevent this.

    Calls the undecorated function directly (bypassing the `@retry` decorator's
    3-attempt/60s-wait loop) since this ValueError is not a transient DB error.
    """
    query = _make_query(LocationScanType.SOURCE_VALUE)
    query.location_scan_type = None
    solver = LocationDistributionQuerySolver(mock_db_client, query)

    with pytest.raises(ValueError, match="location_scan_type"):
        solver.solve_query.__wrapped__(solver, [])
