# Durable deletion propagation

The local deletion and its propagation intent commit in the same SQLite transaction. The
additive `forget_jobs` table holds destination and ids only, including memories promoted from
deleted turns. It never stores the phrase or deleted content. Existing stores and wire records
are unchanged; a database without this table needs no work until its first shared deletion.

Jobs retry on sync, capture passes, and explicit forget calls, including a retry when the phrase
no longer exists locally. An acknowledgement requires the existing transport's actual append
and shared-log scrub to succeed. It does not mistake an ordinary outbox enqueue for delivery.
A crash after publication but before acknowledgement may repeat the notice; existing persistent
forgotten ids make that replay idempotent. Network failures use the existing one-minute backoff.

This fixes newly accepted deletion requests. An old erased request that never left a notice or
job cannot be reconstructed. All known offline peers still need to sync; a completed local job
does not assert every peer has acknowledged. App histories, backups and secure wiping remain
outside this guarantee. Tests and migration checks use synthetic, temporary databases only.
