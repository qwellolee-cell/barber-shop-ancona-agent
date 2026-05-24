# tests/test_local.py — Simulatore di chat locale multi-tenant
# Cleek — Fase 2

"""
Prova il tuo agente senza WhatsApp, simulando una conversazione in terminale.

Uso:
    python tests/test_local.py                    # tenant default (TENANT_SLUG env)
    python tests/test_local.py barber-shop-ancona # tenant specifico
    python tests/test_local.py ristorante-test    # secondo tenant

Comandi nel chat:
    pulisci  — cancella la cronologia della conversazione
    esci     — termina il test
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.brain import generar_respuesta
from agent.memory import (
    inicializar_db,
    guardar_mensaje,
    obtener_historial,
    limpiar_historial,
    get_tenant_by_slug,
    crea_o_aggiorna_tenant,
)
from agent.tenant_loader import carica_tenant, carica_tenant_default

TELEFONO_TEST = "test-local-001"


async def main():
    # Determina il tenant da usare (argomento CLI o env default)
    slug_arg = sys.argv[1] if len(sys.argv) > 1 else None
    default_slug = os.getenv("TENANT_SLUG", "barber-shop-ancona")
    slug = slug_arg or default_slug

    await inicializar_db()

    # Carica config tenant
    try:
        tenant_config = carica_tenant(slug)
    except FileNotFoundError:
        print(f"⚠️  Config tenant '{slug}' non trovata. Uso il default.")
        tenant_config = carica_tenant_default()
        slug = default_slug

    # Recupera o crea tenant nel DB
    db_tenant = await get_tenant_by_slug(slug)
    if db_tenant is None:
        db_tenant = await crea_o_aggiorna_tenant(
            slug=slug,
            nome=tenant_config.nome_business,
            business_type=tenant_config.business_type,
        )
    tenant_id = db_tenant.id

    print()
    print("=" * 60)
    print(f"   Cleek — Test Locale")
    print(f"   Tenant: {tenant_config.nome_business}")
    print(f"   Agente: {tenant_config.nome_agente} {tenant_config.emoji_firma}")
    print(f"   Business: {tenant_config.business_type}")
    print("=" * 60)
    print()
    print("  Scrivi messaggi come se fossi un cliente.")
    print("  Comandi speciali:")
    print("    'pulisci'  — cancella la cronologia")
    print("    'esci'     — termina il test")
    print()
    print("-" * 60)
    print()

    while True:
        try:
            mensaje = input("Tu: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest terminato.")
            break

        if not mensaje:
            continue

        if mensaje.lower() == "esci":
            print("\nTest terminato.")
            break

        if mensaje.lower() == "pulisci":
            await limpiar_historial(TELEFONO_TEST, tenant_id=tenant_id)
            print("[Cronologia cancellata]\n")
            continue

        historial = await obtener_historial(TELEFONO_TEST, tenant_id=tenant_id)

        print(f"\n{tenant_config.nome_agente}: ", end="", flush=True)
        respuesta = await generar_respuesta(
            mensaje,
            historial,
            TELEFONO_TEST,
            tenant_config=tenant_config,
            tenant_id=tenant_id,
        )
        print(respuesta)
        print()

        await guardar_mensaje(TELEFONO_TEST, "user", mensaje, tenant_id=tenant_id)
        await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta, tenant_id=tenant_id)


if __name__ == "__main__":
    asyncio.run(main())
