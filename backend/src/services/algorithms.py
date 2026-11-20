"""Registry of frozen analysis algorithm versions.

A completed trial seals the version string that produced it.  Re-computation
must dispatch through this registry so that an application upgrade can add a
new version without changing how older sealed trials are replayed.
"""

CURRENT_VERSION = 'energy-ridge/1.0.0'

_VERSIONS = {}


class AlgorithmUnavailable(KeyError):
    """The sealed algorithm version is no longer shipped with the application."""


def register(version, function=None):
    """Register (or decorate) an immutable analysis implementation."""

    def decorator(fn):
        _VERSIONS[version] = fn
        return fn

    if function is not None:
        return decorator(function)
    return decorator


def run(version, records, params):
    """Execute a frozen version and stamp the version that actually ran."""
    function = _VERSIONS.get(version)
    if function is None:
        raise AlgorithmUnavailable(version)
    result = function(records, params)
    result['algorithm_version'] = version
    return result


def available():
    return sorted(_VERSIONS)
