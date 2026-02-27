"""
wallet_filter.py – Gestión ESTÁTICA de direcciones de ballenas (EOA + Proxy).

Arquitectura Zero-RPC
---------------------
Todas las direcciones (EOA y Proxy) se cargan estáticamente al inicio
desde config.py / .env.  NO hay descubrimiento dinámico de proxies.
NO se usa eth_getTransactionByHash ni ninguna llamada HTTP/RPC.

Componentes internos:
    1. _wallets:    label → EOA (dirección pública)
    2. _proxies:    label → set[proxy addresses] (cargadas de .env)
    3. _all_lookup: lowercase address → label  (unión de EOAs + proxies)

El método addresses_as_topics() devuelve todas las direcciones como
topics de 32 bytes (left-padded) para usar en eth_subscribe con
filtrado server-side en Alchemy.

Formato .env:
    WHALE_WALLETS=Label:EOA[:proxy1,proxy2];Label2:EOA2[:proxy3]

Ejemplo:
    WHALE_WALLETS=DrPufferfish:0xdb27bf...:0xproxy1,0xproxy2
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class WalletFilter:
    """
    Gestiona las direcciones de ballenas + sus proxy wallets (estáticas).
    """

    def __init__(
        self,
        wallets: dict[str, str] | None = None,
        proxies: dict[str, list[str]] | None = None,
    ):
        """
        Parámetros
        ----------
        wallets : dict[str, str]
            Mapa de  label → dirección EOA (pública)
        proxies : dict[str, list[str]], optional
            Mapa de  label → lista de proxy addresses
        """
        self._wallets: dict[str, str] = {}            # label → EOA
        self._proxies: dict[str, set[str]] = {}       # label → {proxy1, proxy2, …}
        self._all_lookup: dict[str, str] = {}          # lowercase addr → label

        if wallets:
            for label, addr in wallets.items():
                self.add(label, addr)

        if proxies:
            for label, proxy_list in proxies.items():
                for proxy_addr in proxy_list:
                    self.add_proxy(label, proxy_addr)

    # ── CRUD ─────────────────────────────────────────────────────

    def add(self, label: str, address: str) -> None:
        """Añade una wallet EOA a la lista de seguimiento."""
        if not address or not address.startswith("0x") or len(address) != 42:
            logger.warning(
                "Dirección inválida para '%s': '%s'. Ignorada.",
                label, address,
            )
            return
        self._wallets[label] = address
        self._proxies.setdefault(label, set())
        self._all_lookup[address.lower()] = label
        logger.info("🐋 Whale añadida: %s → %s", label, address)

    def add_proxy(self, label: str, proxy_address: str) -> bool:
        """
        Registra una proxy wallet asociada a una ballena.
        Retorna True si es nueva, False si ya existía.
        """
        if label not in self._wallets:
            logger.warning("Label '%s' no registrado. Proxy ignorada.", label)
            return False
        proxy_lower = proxy_address.lower()
        if proxy_lower in self._all_lookup:
            return False  # ya registrada
        self._proxies[label].add(proxy_address)
        self._all_lookup[proxy_lower] = label
        logger.info(
            "🔗 Proxy registrada: %s → %s (proxies=%d)",
            label, proxy_address[:20] + "…", len(self._proxies[label]),
        )
        return True

    def remove(self, label: str) -> None:
        addr = self._wallets.pop(label, None)
        if addr:
            self._all_lookup.pop(addr.lower(), None)
        proxies = self._proxies.pop(label, set())
        for p in proxies:
            self._all_lookup.pop(p.lower(), None)

    # ── Properties ───────────────────────────────────────────────

    @property
    def addresses(self) -> list[str]:
        """Lista de direcciones EOA activas."""
        return list(self._wallets.values())

    @property
    def all_addresses(self) -> list[str]:
        """Todas las direcciones monitorizadas (EOAs + proxies)."""
        return list(self._all_lookup.keys())

    @property
    def labels(self) -> dict[str, str]:
        return dict(self._wallets)

    @property
    def count(self) -> int:
        return len(self._wallets)

    @property
    def proxy_count(self) -> int:
        return sum(len(v) for v in self._proxies.values())

    @property
    def is_empty(self) -> bool:
        return len(self._wallets) == 0

    # ── Matching ─────────────────────────────────────────────────

    def is_target(self, address: str) -> bool:
        """Comprueba si cualquier dirección (EOA o proxy) es target."""
        return address.lower() in self._all_lookup

    def get_label(self, address: str) -> Optional[str]:
        return self._all_lookup.get(address.lower())

    def match_event(
        self, maker: str, taker: str, tx_from: str = "",
    ) -> Optional[tuple[str, str, str]]:
        """
        Comprueba si maker, taker o tx_from es una ballena.

        Retorna
        -------
        (whale_address, whale_label, role) o None.
        role = "maker" | "taker" | "tx_sender"

        Prioridad: maker > taker > tx_from.
        """
        maker_label = self._all_lookup.get(maker.lower())
        if maker_label:
            return (maker, maker_label, "maker")

        taker_label = self._all_lookup.get(taker.lower())
        if taker_label:
            return (taker, taker_label, "taker")

        if tx_from:
            sender_label = self._all_lookup.get(tx_from.lower())
            if sender_label:
                return (tx_from, sender_label, "tx_sender")

        return None

    # ── Topic Filters (solo usados si se filtra por topics) ──────

    def addresses_as_topics(self) -> list[str]:
        """Todas las direcciones (EOAs + proxies) como topics de 32 bytes."""
        topics = []
        for addr_lower in self._all_lookup.keys():
            raw = addr_lower.replace("0x", "").zfill(64)
            topics.append("0x" + raw)
        return topics

    # ── Display ──────────────────────────────────────────────────

    def print_summary(self) -> None:
        print(f"\n  🐋 Ballenas monitorizadas ({self.count} EOAs, {self.proxy_count} proxies):")
        for label, addr in self._wallets.items():
            short = f"{addr[:8]}...{addr[-6:]}"
            proxy_list = self._proxies.get(label, set())
            proxy_info = f" + {len(proxy_list)} proxies" if proxy_list else ""
            print(f"     • {label:20s} → {short}{proxy_info}")
        print()

