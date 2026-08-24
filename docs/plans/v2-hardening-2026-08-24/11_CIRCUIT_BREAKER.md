# Circuit Breaker

For every optional service:
timeout, failure_count, cooldown, next_probe, last_healthy.

Example:
3 failures -> open circuit 60s -> no calls -> one probe -> recover if healthy.

Never retry a dead optional service on every assignment.
