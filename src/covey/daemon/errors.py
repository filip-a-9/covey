"""The one error type the daemon's modules raise. The app turns it into the API error body."""


class CoveyError(Exception):
    def __init__(self, status: int, code: str, message: str, next_step: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.next_step = next_step
