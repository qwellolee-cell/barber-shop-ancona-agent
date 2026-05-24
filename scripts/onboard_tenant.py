#!/usr/bin/env python3
# scripts/onboard_tenant.py — Wizard CLI per onboarding di un nuovo tenant Cleek
# Cleek — Fase 4
"""
Wizard interattivo che guida l'utente nella creazione di un nuovo business tenant.

Genera:
  - tenants/{slug}/settings.yaml   (orari, risorse, reminder)
  - tenants/{slug}/prompts.yaml    (system prompt personalizzato)

E opzionalmente registra il tenant via API REST.

Uso:
    python scripts/onboard_tenant.py
    python scripts/onboard_tenant.py --api http://localhost:8000 --key ADMIN_KEY
"""

import argparse
import os
import re
import sys
import pathlib
import yaml


# ─────────────────────────────────────────────────────────────────────────────
# Costanti / template
# ─────────────────────────────────────────────────────────────────────────────

BUSINESS_TYPES = {
    "1": ("barbiere",   "✂️",  "Barbiere / Parrucchiere"),
    "2": ("ristorante", "🍽️", "Ristorante / Trattoria / Bar"),
    "3": ("estetista",  "💆",  "Centro Estetico / Spa"),
    "4": ("dentista",   "🦷",  "Studio Dentistico / Ambulatorio"),
    "5": ("altro",      "🏪",  "Altro tipo di business"),
}

TONI = {
    "1": "professionale e formale",
    "2": "amichevole e casual",
    "3": "commerciale e persuasivo",
    "4": "empatico e caldo",
}

# Template orari per tipo business
ORARI_TEMPLATE = {
    "barbiere": {
        "slot_granularity_min": 15,
        "reminder_finestre_ore": [24],
        "giorni_chiusi": [0, 6],  # lun, dom
        "turni": [("09:00", "13:00"), ("15:00", "19:00")],
    },
    "ristorante": {
        "slot_granularity_min": 30,
        "reminder_finestre_ore": [2],
        "giorni_chiusi": [0],  # lunedì
        "turni": [("12:00", "15:00"), ("19:00", "23:00")],
    },
    "estetista": {
        "slot_granularity_min": 15,
        "reminder_finestre_ore": [24],
        "giorni_chiusi": [0, 6],  # lun, dom
        "turni": [("09:00", "13:00"), ("14:00", "19:00")],
    },
    "dentista": {
        "slot_granularity_min": 15,
        "reminder_finestre_ore": [48, 2],
        "giorni_chiusi": [5, 6],  # sab, dom
        "turni": [("09:00", "13:00"), ("14:30", "18:30")],
    },
    "altro": {
        "slot_granularity_min": 30,
        "reminder_finestre_ore": [24],
        "giorni_chiusi": [6],  # dom
        "turni": [("09:00", "13:00"), ("15:00", "19:00")],
    },
}

RISORSA_DEFAULT = {
    "barbiere":   {"nome": "Sedia 1", "tipo": "sedia", "capienza": 1},
    "ristorante": {"nome": "Tavolo 1", "tipo": "tavolo", "capienza": 4},
    "estetista":  {"nome": "Cabina 1", "tipo": "cabina", "capienza": 1},
    "dentista":   {"nome": "Studio 1", "tipo": "studio", "capienza": 1},
    "altro":      {"nome": "Risorsa 1", "tipo": "generico", "capienza": 1},
}

NOMI_GIORNI = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers input
# ─────────────────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    """Chiede una stringa all'utente con eventuale default."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\nOnboarding annullato.")
        sys.exit(0)
    return val if val else default


def ask_choice(prompt: str, opzioni: dict, default: str = "1") -> tuple[str, str]:
    """Mostra un menù numerato e restituisce (chiave, valore) scelti."""
    print(f"\n  {prompt}")
    for k, v in opzioni.items():
        marker = "●" if k == default else " "
        label = v[2] if isinstance(v, tuple) else v
        print(f"    {marker} {k}. {label}")
    while True:
        scelta = ask("Scegli", default)
        if scelta in opzioni:
            return scelta, opzioni[scelta]
        print("  ⚠️  Scelta non valida. Inserisci uno dei numeri elencati.")


def slugify(text: str) -> str:
    """Converte una stringa in slug valido per Cleek."""
    s = text.lower().strip()
    s = re.sub(r'[àáâã]', 'a', s)
    s = re.sub(r'[èéêë]', 'e', s)
    s = re.sub(r'[ìíîï]', 'i', s)
    s = re.sub(r'[òóôõ]', 'o', s)
    s = re.sub(r'[ùúûü]', 'u', s)
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s[:50]


def separatore(char: str = "─", width: int = 60):
    print(char * width)


# ─────────────────────────────────────────────────────────────────────────────
# Generazione YAML
# ─────────────────────────────────────────────────────────────────────────────

def genera_orari_yaml(business_type: str, giorni_chiusi: list[int], turni: list[tuple]) -> str:
    """Genera la sezione orari: YAML."""
    linee = ["orari:"]
    for idx, nome in enumerate(NOMI_GIORNI):
        giorno_iso = idx  # 0=lun, 6=dom
        chiuso = giorno_iso in giorni_chiusi
        linee.append(f"  - giorno: {giorno_iso}      # {nome.lower()}")
        if chiuso:
            linee.append("    aperto: false")
        else:
            linee.append("    aperto: true")
            linee.append("    turni:")
            for ap, ch in turni:
                linee.append(f'      - apertura: "{ap}"')
                linee.append(f'        chiusura: "{ch}"')
    return "\n".join(linee)


def genera_settings_yaml(data: dict) -> str:
    """Genera il contenuto di settings.yaml dal dict di dati raccolti."""
    slug         = data["slug"]
    nome         = data["nome"]
    bt           = data["business_type"]
    nome_agente  = data["nome_agente"]
    emoji        = data["emoji"]
    tono         = data["tono"]
    slot_gran    = data["slot_granularity_min"]
    reminder_fin = data["reminder_finestre_ore"]
    giorni_chiusi = data["giorni_chiusi"]
    turni        = data["turni"]
    risorse      = data["risorse"]

    # Reminder template
    if bt == "ristorante":
        reminder_tmpl = (
            f"Ciao {{nome_cliente}}! {emoji}\n"
            "    Ricorda che tra circa {ore}h hai un tavolo prenotato\n"
            "    alle *{ora}* da {nome_business}.\n"
            "    Per cancellare scrivi ANNULLA.\n"
            f"    Ti aspettiamo! {{emoji_firma}}"
        )
    else:
        reminder_tmpl = (
            f"Ciao {{nome_cliente}}! 👋\n"
            "    Ti ricordiamo il tuo appuntamento di domani alle *{ora}*\n"
            "    per *{servizio}* da {nome_business}.\n"
            "    Scrivi *ANNULLA* entro stasera se non puoi venire.\n"
            f"    A presto! {{emoji_firma}}"
        )

    # Sezione risorse YAML
    risorse_linee = ["risorse:"]
    for r in risorse:
        risorse_linee.append(f'  - nome: "{r["nome"]}"')
        risorse_linee.append(f'    tipo: {r["tipo"]}')
        risorse_linee.append(f'    capienza: {r["capienza"]}')
        risorse_linee.append(f'    attiva: true')

    orari_yaml   = genera_orari_yaml(bt, giorni_chiusi, turni)
    risorse_yaml = "\n".join(risorse_linee)

    return f"""# tenants/{slug}/settings.yaml
# Configurazione tenant — generata da Cleek Onboarding
# Business type: {bt}

tenant:
  slug: {slug}
  nome: "{nome}"
  business_type: {bt}
  lingua: it

agente:
  nome: "{nome_agente}"
  tono: "{tono}"
  emoji_firma: "{emoji}"

{orari_yaml}

scheduling:
  slot_granularity_min: {slot_gran}

reminder:
  finestre_ore: {reminder_fin}
  messaggio_template: |
    {reminder_tmpl}

{risorse_yaml}
"""


def genera_prompts_yaml(data: dict) -> str:
    """Genera il contenuto di prompts.yaml."""
    slug        = data["slug"]
    nome        = data["nome"]
    nome_agente = data["nome_agente"]
    emoji       = data["emoji"]
    tono        = data["tono"]
    bt          = data["business_type"]
    servizi     = data.get("servizi_esempio", "")

    # Descrizione capability in base al business type
    if bt == "barbiere":
        capabilities = (
            "- Gestisci prenotazioni per tagli, barbe, trattamenti capelli\n"
            "- Controlla la disponibilità degli slot\n"
            "- Conferma, modifica e cancella appuntamenti"
        )
    elif bt == "ristorante":
        capabilities = (
            "- Gestisci prenotazioni tavoli (pranzo e cena)\n"
            "- Chiedi sempre il numero di coperti\n"
            "- Indica i tavoli disponibili per il numero di persone richiesto\n"
            "- Conferma, modifica e cancella prenotazioni"
        )
    elif bt == "estetista":
        capabilities = (
            "- Gestisci prenotazioni per trattamenti estetici\n"
            "- Controlla la disponibilità delle cabine\n"
            "- Informa sui servizi disponibili e le loro durate\n"
            "- Conferma, modifica e cancella appuntamenti"
        )
    elif bt == "dentista":
        capabilities = (
            "- Gestisci prenotazioni per visite e trattamenti dentistici\n"
            "- Controlla la disponibilità degli studi\n"
            "- Ricorda ai pazienti di portare la documentazione necessaria\n"
            "- Conferma, modifica e cancella appuntamenti"
        )
    else:
        capabilities = (
            "- Gestisci prenotazioni e appuntamenti\n"
            "- Controlla la disponibilità\n"
            "- Conferma, modifica e cancella prenotazioni"
        )

    servizi_section = ""
    if servizi:
        servizi_section = f"\n## Servizi offerti\n{servizi}\n"

    return f"""# tenants/{slug}/prompts.yaml
# System prompt — personalizza questo file per adattare il comportamento dell'agente
# Cleek — tenant: {slug}

system_prompt: |
  Sei {nome_agente}, l'assistente virtuale di {nome}.
  Il tuo tono è {tono}.
  Rispondi sempre in italiano, con messaggi concisi e chiari.

  ## Le tue capacità
  {capabilities}
  {servizi_section}
  ## Regole comportamento
  - Non inventare mai informazioni che non hai
  - Se non sai qualcosa, suggerisci di chiamare direttamente {nome}
  - Usa {emoji} per firmare i messaggi quando appropriato
  - Sii {tono} in ogni risposta
  - Chiedi conferma prima di prenotare: nome, servizio/orario, data e ora

fallback_message: "Scusa, non ho capito bene. Puoi riscrivere? Sono qui per aiutarti {emoji}"
error_message: "Mi dispiace, sto avendo un problema tecnico. Riprova tra qualche minuto!"
"""


# ─────────────────────────────────────────────────────────────────────────────
# Registrazione via API
# ─────────────────────────────────────────────────────────────────────────────

def registra_via_api(data: dict, api_url: str, admin_key: str) -> bool:
    """Registra il tenant tramite POST /admin/tenants."""
    try:
        import httpx
    except ImportError:
        print("  ⚠️  httpx non installato. Esegui: pip install httpx")
        return False

    payload = {
        "slug": data["slug"],
        "nome": data["nome"],
        "business_type": data["business_type"],
        "nome_agente": data["nome_agente"],
        "emoji_firma": data["emoji"],
        "tono": data["tono"],
        "whatsapp_numero": data.get("whatsapp_numero") or None,
        "risorse": data["risorse"],
    }

    try:
        r = httpx.post(
            f"{api_url}/admin/tenants",
            json=payload,
            headers={"X-Admin-Key": admin_key},
            timeout=15,
        )
        if r.status_code == 200:
            resp = r.json()
            print(f"  ✅ Tenant registrato nel DB con ID {resp['tenant']['id']}")
            return True
        else:
            print(f"  ⚠️  API ha risposto {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  ⚠️  Errore connessione API: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Wizard principale
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cleek — Wizard onboarding nuovo tenant",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--api", metavar="URL",
                        help="URL del server Cleek (es. http://localhost:8000)")
    parser.add_argument("--key", metavar="ADMIN_KEY",
                        help="Chiave admin per la registrazione via API")
    args = parser.parse_args()

    # Intestazione
    print()
    separatore("═")
    print("   Cleek — Onboarding Nuovo Tenant")
    separatore("═")
    print()
    print("  Questo wizard genera i file di configurazione per un nuovo")
    print("  business su Cleek. Durata stimata: ~5 minuti.")
    print()
    separatore()
    print()

    data = {}

    # ── STEP 1: Tipo di business ──────────────────────────────
    print("STEP 1 / 7 — Tipo di business")
    scelta, bt_tuple = ask_choice(
        "Che tipo di business è?",
        opzioni={k: (v[0], v[1], f"{v[1]}  {v[2]}") for k, v in BUSINESS_TYPES.items()},
        default="1",
    )
    data["business_type"] = bt_tuple[0]
    print()

    # ── STEP 2: Dati anagrafici ───────────────────────────────
    print("STEP 2 / 7 — Dati del business")
    nome = ask("Nome del business (es. Barber Shop Ancona)")
    while not nome:
        print("  ⚠️  Il nome è obbligatorio.")
        nome = ask("Nome del business")
    data["nome"] = nome

    # Proponi slug
    slug_default = slugify(nome)
    slug = ask(f"Slug URL (solo lettere, cifre, trattini)", slug_default)
    # Valida slug
    while not re.match(r'^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$', slug):
        print("  ⚠️  Slug non valido. Usa solo lettere minuscole, cifre e trattini.")
        slug = ask("Slug URL", slug_default)
    data["slug"] = slug

    wa_numero = ask("Numero WhatsApp del business (opzionale, es. +3912345678)", "")
    data["whatsapp_numero"] = wa_numero or None
    print()

    # ── STEP 3: Agente ────────────────────────────────────────
    print("STEP 3 / 7 — Agente virtuale")
    nomi_default = {
        "barbiere": "Simone", "ristorante": "Sofia",
        "estetista": "Giulia", "dentista": "Marco", "altro": "Assistente",
    }
    emoji_default = {
        "barbiere": "✂️", "ristorante": "🍽️",
        "estetista": "💆", "dentista": "🦷", "altro": "🤖",
    }
    bt = data["business_type"]
    nome_agente = ask("Nome dell'agente", nomi_default.get(bt, "Assistente"))
    emoji       = ask("Emoji firma", emoji_default.get(bt, "🤖"))
    data["nome_agente"] = nome_agente
    data["emoji"]       = emoji

    _, tono_val = ask_choice(
        "Tono di comunicazione dell'agente:",
        opzioni={k: (v, "", f"  {v}") for k, v in TONI.items()},
        default="1",
    )
    data["tono"] = tono_val[0]
    print()

    # ── STEP 4: Orari ─────────────────────────────────────────
    print("STEP 4 / 7 — Orari di apertura")
    tmpl = ORARI_TEMPLATE.get(bt, ORARI_TEMPLATE["altro"])

    # Mostra orari default
    giorni_chiusi_nomi = [NOMI_GIORNI[g] for g in tmpl["giorni_chiusi"]]
    turni_str = " + ".join([f"{a}–{c}" for a, c in tmpl["turni"]])
    print(f"\n  Default per {bt}:")
    print(f"    Orari:         {turni_str}")
    print(f"    Giorni chiusi: {', '.join(giorni_chiusi_nomi)}")
    print(f"    Slot ogni:     {tmpl['slot_granularity_min']} minuti")
    print()

    usa_default = ask("Usa questi orari? (sì/no)", "sì").lower()
    if usa_default in ("sì", "si", "s", "yes", "y"):
        data["giorni_chiusi"]       = tmpl["giorni_chiusi"]
        data["turni"]               = tmpl["turni"]
        data["slot_granularity_min"] = tmpl["slot_granularity_min"]
        data["reminder_finestre_ore"] = tmpl["reminder_finestre_ore"]
    else:
        print("  Inserisci gli orari manualmente (formato HH:MM):")
        ap1 = ask("Apertura turno 1", tmpl["turni"][0][0])
        ch1 = ask("Chiusura turno 1", tmpl["turni"][0][1])
        turni = [(ap1, ch1)]
        secondo_turno = ask("Hai un secondo turno? (sì/no)", "sì").lower()
        if secondo_turno in ("sì", "si", "s", "yes", "y"):
            ap2 = ask("Apertura turno 2", tmpl["turni"][-1][0])
            ch2 = ask("Chiusura turno 2", tmpl["turni"][-1][1])
            turni.append((ap2, ch2))

        print(f"\n  Giorni della settimana: 0=Lun, 1=Mar, 2=Mer, 3=Gio, 4=Ven, 5=Sab, 6=Dom")
        chiusi_input = ask("Numeri giorni chiusi (separati da virgola)", ",".join(str(g) for g in tmpl["giorni_chiusi"]))
        try:
            giorni_chiusi = [int(x.strip()) for x in chiusi_input.split(",") if x.strip().isdigit()]
        except Exception:
            giorni_chiusi = tmpl["giorni_chiusi"]

        slot_gran = ask("Durata slot in minuti", str(tmpl["slot_granularity_min"]))
        try:
            slot_gran = int(slot_gran)
        except Exception:
            slot_gran = tmpl["slot_granularity_min"]

        data["giorni_chiusi"]        = giorni_chiusi
        data["turni"]                = turni
        data["slot_granularity_min"] = slot_gran
        data["reminder_finestre_ore"] = tmpl["reminder_finestre_ore"]
    print()

    # ── STEP 5: Risorse ───────────────────────────────────────
    print("STEP 5 / 7 — Risorse prenotabili")
    tipo_risorsa = {
        "barbiere": "sedia", "ristorante": "tavolo",
        "estetista": "cabina", "dentista": "studio", "altro": "generico",
    }.get(bt, "generico")

    risorsa_def = RISORSA_DEFAULT.get(bt, RISORSA_DEFAULT["altro"])
    print(f"\n  Default: una risorsa di tipo '{tipo_risorsa}'")
    quante = ask("Quante risorse vuoi configurare?", "1")
    try:
        n_risorse = max(1, int(quante))
    except Exception:
        n_risorse = 1

    risorse = []
    for i in range(n_risorse):
        print(f"\n  — Risorsa {i + 1} —")
        nome_r    = ask("Nome", risorsa_def["nome"].replace("1", str(i + 1)))
        tipo_r    = ask("Tipo (sedia/tavolo/cabina/studio/staff/generico)", risorsa_def["tipo"])
        capienza  = ask("Capienza (quante persone)", str(risorsa_def["capienza"]))
        try:
            capienza = int(capienza)
        except Exception:
            capienza = risorsa_def["capienza"]
        risorse.append({"nome": nome_r, "tipo": tipo_r, "capienza": capienza})

    data["risorse"] = risorse
    print()

    # ── STEP 6: Servizi (opzionale) ───────────────────────────
    print("STEP 6 / 7 — Servizi offerti (opzionale)")
    print("  Elenca i servizi che il bot deve conoscere (uno per riga).")
    print("  Premi INVIO due volte per terminare:")
    servizi_linee = []
    while True:
        try:
            linea = input("    ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not linea:
            break
        servizi_linee.append(f"  - {linea}")
    data["servizi_esempio"] = "\n".join(servizi_linee)
    print()

    # ── STEP 7: Riepilogo e conferma ──────────────────────────
    print("STEP 7 / 7 — Riepilogo")
    separatore()
    print(f"  Slug:          {data['slug']}")
    print(f"  Nome:          {data['nome']}")
    print(f"  Business type: {data['business_type']}")
    print(f"  Agente:        {data['nome_agente']} {data['emoji']}")
    print(f"  Tono:          {data['tono']}")
    chiusi_nomi = [NOMI_GIORNI[g] for g in data["giorni_chiusi"]]
    turni_str2  = " + ".join([f"{a}–{c}" for a, c in data["turni"]])
    print(f"  Orari:         {turni_str2}")
    print(f"  Giorni chiusi: {', '.join(chiusi_nomi)}")
    print(f"  Slot:          {data['slot_granularity_min']} min")
    print(f"  Risorse:       {len(data['risorse'])}")
    for r in data["risorse"]:
        print(f"                 - {r['nome']} ({r['tipo']}, cap. {r['capienza']})")
    if data.get("whatsapp_numero"):
        print(f"  WhatsApp:      {data['whatsapp_numero']}")
    separatore()
    print()

    conferma = ask("Creare i file di configurazione? (sì/no)", "sì").lower()
    if conferma not in ("sì", "si", "s", "yes", "y"):
        print("\n  Onboarding annullato.\n")
        sys.exit(0)

    # ── Scrittura file ─────────────────────────────────────────
    tenant_dir = pathlib.Path(f"tenants/{data['slug']}")
    tenant_dir.mkdir(parents=True, exist_ok=True)

    settings_path = tenant_dir / "settings.yaml"
    prompts_path  = tenant_dir / "prompts.yaml"

    # Controlla sovrascrittura
    for path in [settings_path, prompts_path]:
        if path.exists():
            sovr = ask(f"  Il file {path} esiste già. Sovrascrivere? (sì/no)", "no").lower()
            if sovr not in ("sì", "si", "s", "yes", "y"):
                print(f"  ⏭  Saltato: {path}")
                continue

    settings_path.write_text(genera_settings_yaml(data), encoding="utf-8")
    prompts_path.write_text(genera_prompts_yaml(data), encoding="utf-8")

    print()
    print("  ✅ File generati:")
    print(f"     - {settings_path}")
    print(f"     - {prompts_path}")
    print()

    # ── Registrazione API (opzionale) ─────────────────────────
    api_url   = args.api
    admin_key = args.key

    if not api_url:
        reg = ask("Vuoi registrare il tenant nel DB via API? (sì/no)", "no").lower()
        if reg in ("sì", "si", "s", "yes", "y"):
            api_url   = ask("URL del server Cleek", "http://localhost:8000")
            admin_key = ask("Chiave admin (ADMIN_KEY)", os.getenv("ADMIN_KEY", ""))

    if api_url:
        if not admin_key:
            admin_key = ask("Chiave admin (ADMIN_KEY)", "")
        print(f"\n  Registro tenant '{data['slug']}' via API...")
        ok = registra_via_api(data, api_url.rstrip("/"), admin_key)
        if not ok:
            print("  ℹ️  Puoi registrare il tenant manualmente al prossimo avvio del server")
            print(f"     (il DB viene aggiornato automaticamente all'avvio con il nuovo tenant)")

    # ── Istruzioni finali ──────────────────────────────────────
    separatore("═")
    print(f"   Tenant '{data['slug']}' configurato!")
    separatore("═")
    print()
    print("  Prossimi passi:")
    print()
    print(f"  1. Personalizza il system prompt:")
    print(f"     nano tenants/{data['slug']}/prompts.yaml")
    print()
    print(f"  2. Prova il bot in locale:")
    print(f"     python tests/test_local.py {data['slug']}")
    print()
    print("  3. Avvia il server (se non è già in esecuzione):")
    print("     uvicorn agent.main:app --reload --port 8000")
    print()
    print(f"  4. Webhook URL per questo tenant:")
    print(f"     POST /webhook/{data['slug']}")
    print()
    separatore("═")
    print()


if __name__ == "__main__":
    main()
