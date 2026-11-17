"""Evaluation stages as small objects with their dependencies declared up front.

Each stage keeps the metric ``name`` of the Governor method it replaced, so the
stage telemetry and the golden replay stay identical.
"""
