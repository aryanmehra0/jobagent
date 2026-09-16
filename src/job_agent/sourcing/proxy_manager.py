"""Residential Proxy Rotation Manager.

Provides sticky sessions per job board and automatic rotation upon encountering
HTTP 429 (Too Many Requests) or HTTP 403 (Forbidden) rate limits.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Optional, List, Dict
import requests
from rich.console import Console

from job_agent.config.settings import settings

console = Console()


class ProxyManager:
    """Manages residential proxy pools and sticky platform sessions."""

    def __init__(self, proxy_source: Optional[str] = None, proxies_file: Optional[Path] = None):
        self.proxies: List[str] = []
        self.failed_proxies: set[str] = set()
        self.sticky_sessions: Dict[str, str] = {}  # board_name -> proxy_url

        # 1. Load explicit proxy from argument or settings
        explicit_proxy = proxy_source or settings.residential_proxy_url
        if explicit_proxy:
            self.add_proxy(explicit_proxy)

        # 2. Load from proxies file if exists
        target_file = proxies_file or (settings.data_dir / "proxies.txt")
        if target_file.exists():
            with open(target_file, "r", encoding="utf-8") as f:
                for line in f:
                    cleaned = line.strip()
                    if cleaned and not cleaned.startswith("#"):
                        self.add_proxy(cleaned)

    def add_proxy(self, proxy_url: str) -> None:
        """Add a proxy string (http://user:pass@host:port or http://host:port) to pool."""
        normalized = proxy_url.strip()
        if not normalized.startswith(("http://", "https://", "socks5://")):
            normalized = f"http://{normalized}"
        if normalized not in self.proxies:
            self.proxies.append(normalized)

    @property
    def has_proxies(self) -> bool:
        """Whether any valid proxies are loaded."""
        available = [p for p in self.proxies if p not in self.failed_proxies]
        return len(available) > 0

    def get_proxy_for_board(self, board_name: str, force_rotate: bool = False) -> Optional[str]:
        """Retrieve sticky residential proxy for a specific job board.
        
        Preserves the same proxy across multi-page scrapes (sticky session),
        unless force_rotate=True is requested due to a 429/403 error.
        """
        available = [p for p in self.proxies if p not in self.failed_proxies]
        if not available:
            # If all marked failed, reset pool with warning
            if self.proxies:
                console.print("[yellow]⚠ All proxies were previously flagged. Resetting failed list.[/yellow]")
                self.failed_proxies.clear()
                available = list(self.proxies)
            else:
                return None

        # If we already have a sticky session and rotation not requested
        if not force_rotate and board_name in self.sticky_sessions:
            active = self.sticky_sessions[board_name]
            if active in available:
                return active

        # Select a random proxy from available pool to establish sticky session
        chosen = random.choice(available)
        self.sticky_sessions[board_name] = chosen
        return chosen

    def mark_proxy_failed(self, board_name: str, proxy_url: Optional[str] = None) -> Optional[str]:
        """Mark current sticky proxy for board as failed and immediately rotate to a fresh one."""
        target = proxy_url or self.sticky_sessions.get(board_name)
        if target:
            self.failed_proxies.add(target)
            console.print(f"[yellow]⚠ Rotating proxy for {board_name}: flagged {target}[/yellow]")
            if board_name in self.sticky_sessions:
                del self.sticky_sessions[board_name]

        return self.get_proxy_for_board(board_name, force_rotate=True)

    def test_proxy(self, proxy_url: str, timeout: int = 5) -> bool:
        """Test proxy connectivity by pinging an IP check endpoint."""
        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            resp = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=timeout)
            return resp.status_code == 200
        except Exception:
            return False

