# AIQ PostgreSQL read retries

All AIQ read methods share `AIQDatabaseProvider._execute_query()`, including
transaction-ID, UUID and date-range queries. Query SQL, bound parameters and
ticket matching semantics remain unchanged.

The provider defaults to five total attempts. `query_max_attempts` is an
optional constructor argument accepting 1–5. Retried reads use exponential backoff: 0.5, 1.0, 2.0 and 4.0 seconds
after the first through fourth failures, respectively. There is no delay
after the final attempt or a non-transient failure.

Only recovery-conflict `SerializationFailure` messages and specific
`OperationalError` connection-closure/reset messages are retried. Generic
serialization errors, SQL/programming errors, authentication errors, invalid
configuration, connection refusal and deterministic application errors are
not retried. A connection error masking a non-transient query error during
rollback remains non-retryable.

Every attempt opens a new SSL-required connection and sets the existing
read-only transaction setting. The cursor context closes the cursor; explicit
connection closing surrounds the transaction context, including rollback or
commit failures. Both resources are closed before backoff and retry.

Logs identify attempt number, configured maximum and exception class without
logging SQL, parameters or connection credentials. Exhaustion preserves
`AIQDatabaseProviderError` with the final database exception as its cause;
the ticket endpoint retains its existing `AIQ_LOOKUP_FAILED` uncertainty path.

Tests use fake connections/cursors and mocked sleep. They perform no real
PostgreSQL, production, worker or service-startup operations.
