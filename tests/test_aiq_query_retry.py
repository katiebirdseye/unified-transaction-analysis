"""Fake DB connections only: retries, resource cleanup and read semantics."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg2
import pytest

from app.providers import aiq_database_provider as module
from app.providers.aiq_database_provider import AIQDatabaseProvider, AIQDatabaseProviderError

STAMP = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
RECOVERY = 'canceling statement due to conflict with recovery\nDETAIL: User query might have needed to see row versions that must be removed.'
CLOSED = 'SSL connection has been closed unexpectedly'
SOURCE_ROW = dict(transaction_id='0042', yard_id='test', timestamp_utc=STAMP,
    gate_name='Incoming gate', direction='IN', uuid='12345678-1234-1234-1234-123456789abc',
    truck_entered='0010', truck_alisa='0011')


@pytest.fixture
def provider():
    return AIQDatabaseProvider(host='offline.invalid', port=5432, database='offline',
        user='offline', password='fake-password', connect_timeout_seconds=1,
        utc_to_local=lambda stamp, yard: stamp.replace(tzinfo=None)-timedelta(hours=4))


@pytest.fixture
def database(monkeypatch):
    state = SimpleNamespace(errors=[], failure_stage='execute', connection_calls=[],
        connections=[], sleeps=[], executions=[], rows=[dict(SOURCE_ROW)],
        rollback_error=None)
    class Cursor:
        def __init__(self):
            self.closed = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.closed = True
        def execute(self, query, params):
            state.executions.append((query, params))
            if state.failure_stage == 'execute' and current_error:
                raise current_error
        def fetchall(self):
            if state.failure_stage == 'fetch' and current_error:
                raise current_error
            return state.rows
    class Connection:
        def __init__(self, error):
            self.error, self.closed, self.cursor_object = error, False, None
            self.transaction_exits, self.sessions = [], []
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, traceback):
            self.transaction_exits.append(exc_type)
            if exc_type and state.rollback_error:
                raise state.rollback_error
            if state.failure_stage == 'transaction_exit' and self.error:
                raise self.error
        def set_session(self, **kwargs):
            self.sessions.append(kwargs)
            if state.failure_stage == 'session' and self.error:
                raise self.error
        def cursor(self, **kwargs):
            assert kwargs == dict(cursor_factory=module.RealDictCursor)
            self.cursor_object = Cursor()
            return self.cursor_object
        def close(self):
            self.closed = True
    current_error = None
    def connect(**kwargs):
        nonlocal current_error
        state.connection_calls.append(kwargs)
        current_error = state.errors.pop(0) if state.errors else None
        if state.failure_stage == 'connect' and current_error:
            raise current_error
        connection = Connection(current_error)
        state.connections.append(connection)
        return connection
    def sleep(delay):
        # Cleanup must precede backoff and the next connection attempt.
        assert all(conn.closed for conn in state.connections)
        assert all(conn.cursor_object is None or conn.cursor_object.closed for conn in state.connections)
        state.sleeps.append(delay)
    monkeypatch.setattr(module.psycopg2, 'connect', connect)
    monkeypatch.setattr(module.time, 'sleep', sleep)
    return state


@pytest.mark.parametrize('error', [
    psycopg2.errors.SerializationFailure(RECOVERY),
    psycopg2.OperationalError(CLOSED),
    psycopg2.OperationalError('server closed the connection unexpectedly'),
    psycopg2.OperationalError('could not receive data from server: Connection reset by peer'),
])
def test_transient_failure_retries_on_fresh_connection(provider, database, error, caplog):
    database.errors = [error]
    params = ('sensitive parameter',)
    assert provider._execute_query('SELECT fake WHERE id = %s', params) == database.rows
    assert len(database.connections) == 2 and database.connections[0] is not database.connections[1]
    assert database.sleeps == [0.5]
    assert all(conn.closed and conn.cursor_object.closed for conn in database.connections)
    assert all(conn.sessions == [dict(readonly=True)] for conn in database.connections)
    assert all(options['sslmode'] == 'require' for options in database.connection_calls)
    assert database.executions == [('SELECT fake WHERE id = %s', params)]*2
    assert 'attempt 1/5' in caplog.text and type(error).__name__ in caplog.text
    assert 'sensitive parameter' not in caplog.text


@pytest.mark.parametrize('failure_stage', ['connect', 'session', 'fetch', 'transaction_exit'])
def test_transient_failure_at_each_database_stage_is_closed_and_retried(provider, database, failure_stage):
    database.failure_stage = failure_stage
    database.errors = [psycopg2.OperationalError(CLOSED)]
    assert provider._execute_query('SELECT fake', ()) == database.rows
    assert len(database.connection_calls) == 2 and database.sleeps == [0.5]
    assert all(conn.closed for conn in database.connections)
    assert all(conn.cursor_object is None or conn.cursor_object.closed for conn in database.connections)


def test_recovery_error_followed_by_closed_rollback_connection_is_retried(provider, database):
    database.errors = [psycopg2.errors.SerializationFailure(RECOVERY)]
    database.rollback_error = psycopg2.OperationalError(CLOSED)
    assert provider._execute_query('SELECT fake', ()) == database.rows
    assert len(database.connections) == 2 and database.sleeps == [0.5]
    assert all(conn.closed and conn.cursor_object.closed for conn in database.connections)


@pytest.mark.parametrize('original', [psycopg2.errors.SyntaxError('syntax error'),
    psycopg2.errors.InvalidPassword('password authentication failed'), ValueError('bad application input')])
def test_closed_rollback_does_not_hide_non_transient_query_error(provider, database, original):
    database.errors = [original]
    database.rollback_error = psycopg2.OperationalError(CLOSED)
    with pytest.raises(AIQDatabaseProviderError):
        provider._execute_query('SELECT fake', ())
    assert len(database.connections) == 1 and not database.sleeps
    assert database.connections[0].closed and database.connections[0].cursor_object.closed


def test_five_attempts_exhausted_raise_final_provider_error(provider, database, caplog):
    errors = [psycopg2.errors.SerializationFailure(RECOVERY),
              psycopg2.OperationalError(CLOSED), psycopg2.OperationalError(CLOSED),
              psycopg2.OperationalError(CLOSED), psycopg2.OperationalError(CLOSED)]
    database.errors = list(errors)
    with pytest.raises(AIQDatabaseProviderError, match='Could not retrieve AIQ transactions from PostgreSQL') as caught:
        provider._execute_query('SELECT fake', ())
    assert caught.value.__cause__ is errors[-1]
    assert len(database.connection_calls) == 5 and database.sleeps == [0.5, 1.0, 2.0, 4.0]
    assert all(conn.closed and conn.cursor_object.closed for conn in database.connections)
    assert 'attempt 5/5' in caplog.text


def test_success_on_third_attempt_has_bounded_backoff(provider, database):
    database.errors = [psycopg2.errors.SerializationFailure(RECOVERY), psycopg2.OperationalError(CLOSED)]
    assert provider._execute_query('SELECT fake', ()) == database.rows
    assert len(database.connection_calls) == 3 and database.sleeps == [0.5, 1.0]


def test_configured_two_attempt_limit(database):
    provider = AIQDatabaseProvider(host='offline.invalid', port=5432, database='offline',
        user='offline', password='fake', connect_timeout_seconds=1,
        utc_to_local=lambda stamp, yard: stamp, query_max_attempts=2)
    database.errors = [psycopg2.OperationalError(CLOSED)]*3
    with pytest.raises(AIQDatabaseProviderError):
        provider._execute_query('SELECT fake', ())
    assert len(database.connection_calls) == 2 and database.sleeps == [0.5]


@pytest.mark.parametrize('error', [
    psycopg2.errors.SyntaxError('syntax error'),
    psycopg2.ProgrammingError('unknown column'),
    psycopg2.errors.InvalidPassword('password authentication failed'),
    psycopg2.errors.InvalidAuthorizationSpecification('authentication denied; connection is closed'),
    psycopg2.OperationalError('password authentication failed; SSL connection has been closed unexpectedly'),
    psycopg2.OperationalError('no pg_hba.conf entry; connection is closed'),
    psycopg2.OperationalError('connection refused'),
    psycopg2.errors.SerializationFailure('could not serialize access due to concurrent update'),
    ValueError('deterministic application error: connection is closed'),
])
def test_non_transient_failure_is_not_retried(provider, database, error):
    database.errors = [error]
    with pytest.raises(AIQDatabaseProviderError) as caught:
        provider._execute_query('SELECT fake', ())
    assert caught.value.__cause__ is error
    assert len(database.connection_calls) == 1 and not database.sleeps
    assert database.connections[0].closed and database.connections[0].cursor_object.closed


@pytest.mark.parametrize('query_max_attempts', [0, 6, -1, 1.5, True])
def test_invalid_retry_configuration_performs_no_reads(database, query_max_attempts):
    with pytest.raises(ValueError, match='query_max_attempts'):
        AIQDatabaseProvider(host='offline.invalid', port=5432, database='offline',
            user='offline', password='fake', connect_timeout_seconds=1,
            utc_to_local=lambda stamp, yard: stamp, query_max_attempts=query_max_attempts)
    assert not database.connection_calls


def test_missing_database_configuration_performs_no_reads(database):
    with pytest.raises(ValueError, match='Missing AIQ PostgreSQL configuration'):
        AIQDatabaseProvider(host='', port=5432, database='offline', user='offline', password='fake',
            connect_timeout_seconds=1, utc_to_local=lambda stamp, yard: stamp)
    assert not database.connection_calls


def test_exact_transaction_id_read_uses_retry_and_preserves_string_identity(provider, database):
    database.errors = [psycopg2.errors.SerializationFailure(RECOVERY)]
    records = provider.get_transactions_by_id(transaction_id=' 0042 ')
    assert len(database.connections) == 2 and database.sleeps == [0.5]
    assert records[0].ground_truth_transaction_id == '0042'
    assert records[0].gate_name == 'Incoming gate' and records[0].timestamp_utc == STAMP
    assert records[0].ground_truth_truck_number == '0010'
    assert all(params == ('0042',) for _, params in database.executions)
    assert database.executions[0] == database.executions[1]


def test_date_range_read_uses_retry_without_changing_scope_or_filters(provider, database):
    database.errors = [psycopg2.OperationalError(CLOSED)]
    start, end = STAMP-timedelta(hours=1), STAMP+timedelta(hours=1)
    records = provider.get_transactions(yard_ids=[' TEST '], start_utc=start, end_utc=end)
    assert len(records) == 1 and records[0].ground_truth_transaction_id == '0042'
    assert database.executions[0] == database.executions[1]
    sql, params = database.executions[0]
    assert params == (['test'], start.replace(tzinfo=None), end.replace(tzinfo=None))
    assert "tt.status = 'FINISHED'" in sql
    assert 'LEFT JOIN public.truck_transaction_automation tta' in sql
    assert provider.ALISA_SOURCE_FILTER not in sql
    assert 'tt.started >= %s' in sql and 'tt.started < %s' in sql
    assert records[0].timestamp_utc == STAMP and records[0].direction == 'IN'


def test_successful_empty_read_does_not_retry(provider, database):
    database.rows = []
    assert provider._execute_query('SELECT fake', ()) == []
    assert len(database.connections) == 1 and not database.sleeps
    assert database.connections[0].closed


@pytest.mark.parametrize('successful_attempt', [4, 5])
def test_success_on_later_attempt_uses_exponential_backoff(provider, database, successful_attempt):
    database.errors = [psycopg2.OperationalError(CLOSED) for _ in range(successful_attempt - 1)]
    records = provider.get_transactions_by_id(transaction_id=' 0042 ')
    assert records[0].ground_truth_transaction_id == '0042'
    assert len(database.connection_calls) == successful_attempt
    assert database.sleeps == [0.5, 1.0, 2.0, 4.0][:successful_attempt - 1]
    assert len({id(conn) for conn in database.connections}) == successful_attempt
    assert all(conn.closed and conn.cursor_object.closed for conn in database.connections)
    assert all(params == ('0042',) for _, params in database.executions)
