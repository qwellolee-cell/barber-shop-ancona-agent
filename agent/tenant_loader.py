# agent/tenant_loader.py — Carica e cache la configurazione di un tenant
# Cleek — Fase 1

"""
TenantConfig: oggetto immutabile che rappresenta la configurazione completa
di un singolo tenant. Viene caricato all'avvio del server e messo in cache.

In Fase 1 supporta il caricamento da file YAML (tenants/{slug}/settings.yaml).
In Fase 2 sarà esteso per leggere anche dal DB (tabella tenants).

Utilizzo:
    from agent.tenant_loader import carica_tenant_default, TenantConfig

    config = carica_tenant_default()
    orari = config.orari_apertura()
    granularita = config.slot_granularity_min
"""

import os
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import time
from typing import Optional

import yaml

logger = logging.getLogger("agentkit")


@dataclass(frozen=True)
class TurnoApertura:
    """Un singolo turno di apertura (mattina o pomeriggio)."""
    apertura: time     # es. 09:00
    chiusura: time     # es. 13:00


@dataclass(frozen=True)
class GiornoOrario:
    """Orari di apertura di un singolo giorno della settimana."""
    giorno: int                              # 0=lunedì … 6=domenica
    aperto: bool
    turni: tuple[TurnoApertura, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TenantConfig:
    """
    Configurazione immutabile di un tenant.
    Tutti i valori sono già parsati e pronti all'uso.
    """
    slug: str
    nome_business: str
    business_type: str
    lingua: str
    nome_agente: str
    emoji_firma: str

    # Scheduling
    slot_granularity_min: int
    reminder_finestre_ore: tuple[int, ...]
    reminder_messaggio_template: str

    # Orari (7 giorni, indice = giorno ISO 0=lun)
    _orari: tuple[GiornoOrario, ...]

    def orari_per_giorno(self, giorno_iso: int) -> GiornoOrario:
        """Restituisce la configurazione orari per un giorno (0=lunedì…6=domenica)."""
        for g in self._orari:
            if g.giorno == giorno_iso:
                return g
        # Giorno non configurato → chiuso per default
        return GiornoOrario(giorno=giorno_iso, aperto=False, turni=())

    def is_giorno_chiuso(self, giorno_iso: int) -> bool:
        """True se il negozio è chiuso nel giorno indicato."""
        return not self.orari_per_giorno(giorno_iso).aperto

    def turni_apertura(self, giorno_iso: int) -> list[TurnoApertura]:
        """Lista di turni aperti per quel giorno."""
        g = self.orari_per_giorno(giorno_iso)
        if not g.aperto:
            return []
        return list(g.turni)

    def formatta_messaggio_reminder(self, **kwargs) -> str:
        """
        Formatta il messaggio reminder con i parametri del tenant.
        Keyword args: nome_cliente, ora, servizio, data, num_persone, ...
        """
        kwargs.setdefault("nome_business", self.nome_business)
        kwargs.setdefault("emoji_firma", self.emoji_firma)
        try:
            return self.reminder_messaggio_template.format(**kwargs)
        except KeyError as e:
            logger.warning(f"Chiave mancante nel template reminder: {e}")
            return self.reminder_messaggio_template


def _parse_time(s: str) -> time:
    """Converte stringa 'HH:MM' in oggetto time."""
    h, m = s.split(":")
    return time(int(h), int(m))


def _parse_orari(orari_raw: list[dict]) -> tuple[GiornoOrario, ...]:
    """Converte la lista YAML degli orari in tuple di GiornoOrario."""
    giorni = []
    for g in orari_raw:
        turni_raw = g.get("turni", [])
        turni = []
        for t in turni_raw:
            if isinstance(t, dict):
                turni.append(TurnoApertura(
                    apertura=_parse_time(t["apertura"]),
                    chiusura=_parse_time(t["chiusura"]),
                ))
            elif isinstance(t, str) and "-" in t:
                # Formato compatto: "09:00-13:00"
                apertura_s, chiusura_s = t.split("-")
                turni.append(TurnoApertura(
                    apertura=_parse_time(apertura_s.strip()),
                    chiusura=_parse_time(chiusura_s.strip()),
                ))
        giorni.append(GiornoOrario(
            giorno=g["giorno"],
            aperto=g.get("aperto", False),
            turni=tuple(turni),
        ))
    return tuple(giorni)


def _carica_yaml_tenant(slug: str) -> dict:
    """Legge il file settings.yaml del tenant indicato."""
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base_dir, "tenants", slug, "settings.yaml")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config tenant non trovata: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    logger.debug(f"Config tenant caricata da: {path}")
    return data


def _build_tenant_config(slug: str, data: dict) -> TenantConfig:
    """Costruisce un TenantConfig dal dict YAML parsato."""
    tenant_section = data.get("tenant", {})
    agente_section = data.get("agente", {})
    scheduling_section = data.get("scheduling", {})
    reminder_section = data.get("reminder", {})
    orari_raw = data.get("orari", [])

    return TenantConfig(
        slug=slug,
        nome_business=tenant_section.get("nome", slug),
        business_type=tenant_section.get("business_type", "generico"),
        lingua=tenant_section.get("lingua", "it"),
        nome_agente=agente_section.get("nome", "Assistente"),
        emoji_firma=agente_section.get("emoji_firma", ""),
        slot_granularity_min=scheduling_section.get("slot_granularity_min", 30),
        reminder_finestre_ore=tuple(reminder_section.get("finestre_ore", [24])),
        reminder_messaggio_template=reminder_section.get(
            "messaggio_template",
            "Ciao {nome_cliente}! Appuntamento domani alle {ora} da {nome_business}."
        ),
        _orari=_parse_orari(orari_raw),
    )


@lru_cache(maxsize=32)
def carica_tenant(slug: str) -> TenantConfig:
    """
    Carica e mette in cache la configurazione di un tenant per slug.
    Il risultato è memorizzato in memoria per tutta la durata del processo.
    Per invalidare la cache (es. dopo un aggiornamento) chiamare
    carica_tenant.cache_clear().
    """
    data = _carica_yaml_tenant(slug)
    config = _build_tenant_config(slug, data)
    logger.info(f"TenantConfig caricata: {slug} ({config.business_type})")
    return config


def carica_tenant_default() -> TenantConfig:
    """
    Carica il tenant di default dal env TENANT_SLUG.
    Se non configurato, usa 'barber-shop-ancona' (compatibilità backward).
    """
    slug = os.getenv("TENANT_SLUG", "barber-shop-ancona")
    return carica_tenant(slug)
