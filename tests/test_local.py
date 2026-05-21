import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, limpiar_historial

TELEFONO_TEST = "test-local-001"


async def main():
    await inicializar_db()

    print()
    print("=" * 55)
    print("   AgentKit — Test Local | Barber Shop Ancona")
    print("=" * 55)
    print()
    print("  Scrivi messaggi come se fossi un cliente.")
    print("  Comandi speciali:")
    print("    'pulisci'  — cancella la cronologia")
    print("    'esci'     — termina il test")
    print()
    print("-" * 55)
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
            await limpiar_historial(TELEFONO_TEST)
            print("[Cronologia cancellata]\n")
            continue

        historial = await obtener_historial(TELEFONO_TEST)

        print("\nSimone: ", end="", flush=True)
        respuesta = await generar_respuesta(mensaje, historial, TELEFONO_TEST)
        print(respuesta)
        print()

        await guardar_mensaje(TELEFONO_TEST, "user", mensaje)
        await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta)


if __name__ == "__main__":
    asyncio.run(main())
