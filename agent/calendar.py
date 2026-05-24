# agent/calendar.py — Logica di calendario configurabile per tenant
# Cleek — Fase 1

"""
Espone le costanti di calendario del tenant corrente (backward compatibility)
e le funzioni di utilità per i calcoli di disponibilità.

Le costanti ORARIO_NEGOCIO e GIORNI_CHIUSI sono mantenute solo per
compatibilità con codice legacy. Il nuovo codice deve usare TenantConfig
via agent.tenant_loader.

Migrazione in corso:
  PRIMA (hardcoded):
      from agent.calendar import ORARIO_NEGOCIO, GIORNI_CHIUSI

  DOPO (configurabile):
      from agent.tenant_loader import carica_tenant_default
      config = carica_tenant_default()
      turni = config.turni_apertura(giorno_iso)
"""

import logging
from datetime import time
from agent.tenant_loader import carica_tenant_default, TurnoApertura

logger = logging.getLogger("agentkit")

# ---------------------------------------------------------------------------
# Costanti legacy — derivate dalla config del tenant default
# Mantenute per compatibilità backward con memory.py (Fase 1)
# Verranno rimosse in Fase 2 quando memory.py sarà multi-tenant
# ---------------------------------------------------------------------------

def _build_legacy_constants():
    """
    Costruisce le costanti in formato legacy dalla config del tenant default.
    Chiamata una sola volta all'import.
    """
    try:
        config = carica_tenant_default()

        # ORARIO_NEGOCIO: lista di tuple ((h_apertura, m), (h_chiusura, m))
        # Prende i turni del primo giorno aperto come riferimento,
        # ma in realtà usa tutti i turni configurati nell'orario settimanale
        # raccogliendoli in un set unico (i barbieri hanno orari identici ogni giorno)
        turni_unici: list[tuple[tuple[int, int], tuple[int, int]]] = []
        visti = set()
        for giorno_iso in range(7):
            for t in config.turni_apertura(giorno_iso):
                chiave = (t.apertura.hour, t.apertura.minute,
                          t.chiusura.hour, t.chiusura.minute)
                if chiave not in visti:
                    visti.add(chiave)
                    turni_unici.append((
                        (t.apertura.hour, t.apertura.minute),
                        (t.chiusura.hour, t.chiusura.minute),
                    ))

        # GIORNI_CHIUSI: lista di nomi italiani (in minuscolo, senza accenti)
        NOMI_IT = {0: "lunedi", 1: "martedi", 2: "mercoledi",
                   3: "giovedi", 4: "venerdi", 5: "sabato", 6: "domenica"}
        chiusi = [
            NOMI_IT[giorno_iso]
            for giorno_iso in range(7)
            if config.is_giorno_chiuso(giorno_iso)
        ]

        return turni_unici, chiusi

    except Exception as e:
        # Se la config non è disponibile (es. durante i test senza file YAML),
        # ritorna i valori originali di Barber Shop Ancona come fallback
        logger.warning(f"Config tenant non caricata, uso fallback hardcoded: {e}")
        return [
            ((9, 0), (13, 0)),
            ((15, 0), (19, 0)),
        ], ["lunedi", "domenica"]


ORARIO_NEGOCIO, GIORNI_CHIUSI = _build_legacy_constants()
