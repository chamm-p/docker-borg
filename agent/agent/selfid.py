"""Robuste Erkennung des eigenen Container-Objekts.

socket.gethostname() == Container-ID gilt NUR, wenn niemand den Hostnamen
überschreibt. QNAP Container Station (und compose `hostname:`) tun genau das —
dann schlägt containers.get(hostname) fehl und alles, was darauf aufbaut
(agent-data-Volume-Erkennung, Self-Skip), fällt still auf falsche Defaults.

Zuverlässiger: die eigene Container-ID aus /proc/self/mountinfo lesen — Docker
mountet /etc/hostname & Co. aus /var/lib/docker/containers/<id>/..., die ID
steht damit im Mount-Pfad. Fallback: /proc/self/cgroup, dann hostname.
"""
from __future__ import annotations

import logging
import re
import socket

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"/containers/([0-9a-f]{64})/")
_CGROUP_ID_RE = re.compile(r"([0-9a-f]{64})")


def own_container_id() -> str | None:
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                m = _ID_RE.search(line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                m = _CGROUP_ID_RE.search(line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def own_container(docker_client):
    """Das eigene Container-Objekt, oder None. Versucht erst die robuste ID,
    dann den Hostnamen (Standard-Docker ohne hostname-Override)."""
    for candidate in (own_container_id(), socket.gethostname()):
        if not candidate:
            continue
        try:
            return docker_client.containers.get(candidate)
        except Exception:  # noqa: BLE001
            continue
    logger.warning("Eigener Container nicht identifizierbar (mountinfo/cgroup/hostname)")
    return None
