"""Core logic: database, video processing, search, fingerprinting, settings.

Heavy modules (video, fingerprint) are imported on demand.
"""

from . import config, db, models, search, settings  # noqa: F401
# from . import video, fingerprint  # lazy
