"""Hosted deployment helpers.

The local flow console remains loopback-only. This package contains the safe
control-plane pieces a public deployment can expose: health/readiness endpoints
and a durable run queue for isolated workers.
"""
