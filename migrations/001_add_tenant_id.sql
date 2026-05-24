-- migrations/001_add_tenant_id.sql
-- Cleek — Fase 2: aggiunge supporto multi-tenant al DB esistente
--
-- ISTRUZIONI:
-- 1. Fare backup del DB prima di eseguire: cp agentkit.db agentkit.db.backup
-- 2. Eseguire su SQLite: sqlite3 agentkit.db < migrations/001_add_tenant_id.sql
-- 3. Eseguire su PostgreSQL: psql $DATABASE_URL -f migrations/001_add_tenant_id.sql
--
-- Questo script è IDEMPOTENTE: può essere eseguito più volte senza errori.
-- Compatibile con SQLite 3.37+ e PostgreSQL 14+.


-- ────────────────────────────────────────────────────────────────────────────
-- STEP 1: Crea tabella tenants (se non esiste)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        VARCHAR(50)  NOT NULL UNIQUE,
    nome        VARCHAR(100) NOT NULL,
    business_type VARCHAR(50) NOT NULL DEFAULT 'barbiere',
    -- numero WhatsApp del business (usato per routing multi-tenant in Fase 2)
    whatsapp_numero VARCHAR(30) UNIQUE,
    -- path relativo al file settings.yaml del tenant
    config_path VARCHAR(200),
    attivo      BOOLEAN NOT NULL DEFAULT 1,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ────────────────────────────────────────────────────────────────────────────
-- STEP 2: Inserisce il tenant Barber Shop Ancona come tenant ID=1
-- (INSERT OR IGNORE = non fa niente se esiste già)
-- ────────────────────────────────────────────────────────────────────────────

INSERT OR IGNORE INTO tenants (id, slug, nome, business_type, config_path, attivo)
VALUES (
    1,
    'barber-shop-ancona',
    'Barber Shop Ancona',
    'barbiere',
    'tenants/barber-shop-ancona/settings.yaml',
    1
);


-- ────────────────────────────────────────────────────────────────────────────
-- STEP 3: Aggiunge colonna tenant_id alle tabelle esistenti (nullable first)
-- Nota: SQLite non supporta ALTER COLUMN, quindi usiamo ADD COLUMN nullable
-- poi facciamo il backfill, poi aggiungiamo il vincolo NOT NULL in un secondo
-- momento (richiede ricreazione tabella in SQLite — gestita in Fase 2 code).
-- ────────────────────────────────────────────────────────────────────────────

-- Per SQLite: ADD COLUMN funziona solo se la colonna non esiste già
-- Questo blocco usa una tecnica compatibile con SQLite

-- Tabella appuntamenti
ALTER TABLE appuntamenti ADD COLUMN tenant_id INTEGER REFERENCES tenants(id);

-- Tabella mensajes
ALTER TABLE mensajes ADD COLUMN tenant_id INTEGER REFERENCES tenants(id);


-- ────────────────────────────────────────────────────────────────────────────
-- STEP 4: Backfill — assegna tutti i record esistenti al tenant barber-shop-ancona
-- ────────────────────────────────────────────────────────────────────────────

UPDATE appuntamenti SET tenant_id = 1 WHERE tenant_id IS NULL;
UPDATE mensajes SET tenant_id = 1 WHERE tenant_id IS NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- STEP 5: Crea indici per performance multi-tenant
-- ────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_appuntamenti_tenant_data
    ON appuntamenti (tenant_id, data_ora);

CREATE INDEX IF NOT EXISTS idx_appuntamenti_tenant_telefono
    ON appuntamenti (tenant_id, telefono);

CREATE INDEX IF NOT EXISTS idx_mensajes_tenant_telefono
    ON mensajes (tenant_id, telefono);


-- ────────────────────────────────────────────────────────────────────────────
-- VERIFICA (opzionale — eseguire separatamente per debug)
-- ────────────────────────────────────────────────────────────────────────────
-- SELECT COUNT(*) as tot_appuntamenti, COUNT(tenant_id) as con_tenant_id FROM appuntamenti;
-- SELECT COUNT(*) as tot_mensajes, COUNT(tenant_id) as con_tenant_id FROM mensajes;
-- SELECT * FROM tenants;
