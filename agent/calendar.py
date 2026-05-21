# agent/calendar.py — Costanti e logica di calendario per Barber Shop Ancona

# Turni di apertura come lista di tuple (ora_apertura, ora_chiusura)
# Ogni elemento: ((ora, minuto), (ora, minuto))
ORARIO_NEGOCIO: list[tuple[tuple[int, int], tuple[int, int]]] = [
    ((9, 0), (13, 0)),   # mattina
    ((15, 0), (19, 0)),  # pomeriggio
]

# Giorni chiusi in italiano (minuscolo, senza accenti)
GIORNI_CHIUSI: list[str] = ["lunedi", "domenica"]
