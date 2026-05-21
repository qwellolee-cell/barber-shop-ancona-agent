import os
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")


def cargar_info_negocio() -> dict:
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "Non disponibile"),
        "esta_abierto": True,
    }


def buscar_en_knowledge(consulta: str) -> str:
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "Nessun file di conoscenza disponibile."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "Non ho trovato informazioni specifiche su questo nei miei file."


def confirmar_cita(telefono: str, dia: str, hora: str, servicio: str) -> str:
    """Genera el mensaje de confirmación de cita."""
    return (
        f"Perfetto! ✅ Ho fissato il tuo appuntamento:\n"
        f"📅 {dia} alle {hora}\n"
        f"✂️ {servicio}\n"
        f"📍 Barber Shop Ancona\n"
        f"Ti aspettiamo!"
    )


def obtener_servicios() -> str:
    """Retorna la lista de servicios disponibles."""
    return (
        "I nostri servizi:\n\n"
        "✂️ *Taglio e Styling*\n"
        "• Taglio Uomo (30 min)\n"
        "• Taglio Uomo con Shampoo (35 min)\n"
        "• Taglio Bambino fino 10 anni (20 min)\n"
        "• Sfumatura e Dettagli (15 min)\n\n"
        "🧔 *Cura della Barba*\n"
        "• Sistemazione Barba (20 min)\n"
        "• Taglio e Design Barba (30 min)\n"
        "• Trattamento Barba Deluxe (40 min)\n\n"
        "✨ *Pacchetti*\n"
        "• Rituale di Rasatura Classico (30 min)\n"
        "• Taglio + Barba Experience – *€25* (60 min) ⭐\n\n"
        "Quale ti interessa?"
    )
