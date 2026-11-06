"""Custom exceptions for kvdb engine."""


class DiskFullError(Exception):
    """Raised when an operation would exceed max_bytes cap or violate reserve."""
    pass


class DBPoisonedError(Exception):
    """Raised when the database instance is poisoned due to an earlier fsync or truncate failure."""
    pass


class CorruptFileError(Exception):
    """Raised when database header or critical structures are unrecoverably corrupted."""
    pass
