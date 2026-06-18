"""Recovery strategy placeholder."""


class RecoveryStrategy:
    def should_retry(self, error: Exception) -> bool:
        return False
