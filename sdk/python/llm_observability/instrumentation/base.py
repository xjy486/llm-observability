"""Base instrumentor abstract class."""
from abc import ABC, abstractmethod


class BaseInstrumentor(ABC):
    """Base class for auto-instrumentation.

    Subclasses implement instrument() to patch a library and
    uninstrument() to restore the original.
    """

    _patched = False

    @abstractmethod
    def instrument(self, **kwargs):
        """Patch the target library."""
        pass

    @abstractmethod
    def uninstrument(self):
        """Restore the original library behavior."""
        pass

    @property
    def is_patched(self) -> bool:
        return self._patched
