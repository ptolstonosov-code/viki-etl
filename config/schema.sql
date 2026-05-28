-- ─────────────────────────────────────────────────────────────────────────────
-- TARGET DATABASE SCHEMA (source of truth for LLM prompt)
-- Generated from dbdiagram.io v3, adapted to SQLite-compatible DDL.
-- This file is shown to the LLM verbatim so it understands the target schema.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Каталог товаров ──────────────────────────────────────────────────────────

CREATE TABLE catalog_group (
  id          TEXT PRIMARY KEY,                                  -- UUID группы (v4)
  parent_id   TEXT,                                              -- Дерево категорий
  name        TEXT NOT NULL,                                     -- Наименование категории
  created_at  TEXT DEFAULT (datetime('now')),                    -- Дата создания
  FOREIGN KEY (parent_id) REFERENCES catalog_group(id)
);
-- Иерархия: корень → разделы → подразделы. Рекомендуется глубина ≤3.

CREATE TABLE catalog_group_attribute (
  id          TEXT PRIMARY KEY,                                  -- UUID группы
  description TEXT,                                              -- Развёрнутое описание для карточки
  FOREIGN KEY (id) REFERENCES catalog_group(id)
);

CREATE TABLE catalog_product (
  id          TEXT PRIMARY KEY,                                  -- UUID шаблона товара
  name        TEXT NOT NULL,                                     -- Общее наименование (без характеристик)
  type        TEXT CHECK (type IN ('default','dish','service')), -- Тип товара
  tax_rate    TEXT NOT NULL CHECK (tax_rate IN ('NDS_NO_TAX','NDS_5','NDS_7','NDS_10','NDS_5_105','NDS_7_107','NDS_10_110','NDS_22','NDS_22_122')),  -- Ставка НДС
  group_id    TEXT NOT NULL,                                     -- Группа товара
  unit        TEXT NOT NULL CHECK (unit IN ('796','163','166','168','004','005','006','051','053','055','111','112','113','245','233','359','356','355','354','256','257','2553','2554')),  -- ОКЕИ
  created_at  TEXT DEFAULT (datetime('now')),
  archived_at TEXT,                                              -- NULL = активен
  excise      INTEGER DEFAULT 0,                                 -- Подакцизный товар
  compound    INTEGER DEFAULT 0,                                 -- Составной товар (набор)
  description TEXT,
  marked      INTEGER,                                           -- Признак маркировки
  alcohol     INTEGER,                                           -- Признак алкоголя
  FOREIGN KEY (group_id) REFERENCES catalog_group(id)
);
-- Шаблон товара: общая информация, не зависящая от характеристик.

CREATE TABLE catalog_variant (
  id           TEXT PRIMARY KEY,                                 -- UUID варианта (SKU)
  product_id   TEXT NOT NULL,
  display_name TEXT,                                             -- Название с учётом характеристик
  created_at   TEXT DEFAULT (datetime('now')),
  archived_at  TEXT,
  FOREIGN KEY (product_id) REFERENCES catalog_product(id)
);

CREATE TABLE variant_price (
  variant_id  TEXT PRIMARY KEY,                                  -- 1:1 к варианту
  price       INTEGER NOT NULL,                                  -- Розничная цена в КОПЕЙКАХ (≥0)
  updated_at  TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);
-- Текущая цена. История — в doc_price_change_positions.

CREATE TABLE variant_barcode (
  variant_id  TEXT PRIMARY KEY,
  barcode     TEXT UNIQUE NOT NULL,                              -- Значение штрихкода
  type_pack   TEXT CHECK (type_pack IN ('inner_pack','trade_unit','box','pallet','layer','metro_unit','show_pack')),
  multiplier  INTEGER,                                           -- Кол-во единиц в type_pack
  type        TEXT DEFAULT 'EAN13' CHECK (type IN ('EAN13','EAN8','ITF14','Code128')),
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);

CREATE TABLE variant_image (
  id          TEXT PRIMARY KEY,
  variant_id  TEXT,
  miniature   TEXT,                                              -- JSON: формат фото
  tiny        TEXT,                                              -- JSON: миниатюра
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);

CREATE TABLE variant_article (
  variant_id  TEXT PRIMARY KEY,
  article     TEXT UNIQUE NOT NULL,                              -- Артикул
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);

CREATE TABLE variant_marked_meta (
  variant_id  TEXT PRIMARY KEY,
  "group"     TEXT CHECK ("group" IN ('lp','shoes','tobacco','perfumery','tires','electronics','milk','bicycle','wheelchairs','alcohol','otp','water','furs','beer','ncp','bio','antiseptic','petfood','seafood','nabeer','softdrinks','vetpharma','toys','radio','titan','conserve','vegetableoil','opticfiber','chemistry','books','grocery','pharmaraw','construction','fire','heater','cableraw','autofluids','polymer','sweets','carparts')),
  osu         INTEGER,                                           -- Учитывается по ОСУ?
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);

CREATE TABLE variant_alcohol_meta (
  variant_id    TEXT PRIMARY KEY,
  alc_type_code TEXT,                                            -- Тип АП (пример: '200' = водка)
  alc_volume    NUMERIC,                                         -- Ёмкость (например 0.5)
  alc_unitType  TEXT CHECK (alc_unitType IN ('PACKED','UNPAKED')),
  alc_code      TEXT,                                            -- JSON: алкокоды
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);

CREATE TABLE variant_sync_status (
  variant_id  TEXT,
  status      TEXT CHECK (status IN ('PENDING','ERROR')),
  message     TEXT,
  device_id   TEXT,                                              -- ID кассы
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);

CREATE TABLE variant_egais_mark (
  id                  TEXT PRIMARY KEY,
  variant_id          TEXT,
  mark                TEXT UNIQUE,                               -- Алкогольная марка
  actual              INTEGER,                                   -- В обороте?
  income_document_id  TEXT,
  created_at          TEXT,
  outcome_document_id TEXT,
  updated_at          TEXT,
  forma               TEXT,                                      -- Форма А
  formb               TEXT,                                      -- Форма B
  alccode             TEXT,
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);

CREATE TABLE variant_thuemark_mark (
  id                  TEXT PRIMARY KEY,
  variant_id          TEXT,
  parent_mark         TEXT UNIQUE,                               -- Марка родителя (ЧЗ)
  child_mark          TEXT UNIQUE,                               -- Марка ребёнка
  actual              INTEGER,
  income_document_id  TEXT,
  created_at          TEXT,
  outcome_document_id TEXT,
  updated_at          TEXT,
  type_pack           TEXT CHECK (type_pack IN ('inner_pack','trade_unit','box','pallet','layer','metro_unit','show_pack')),
  multiplier          INTEGER,
  FOREIGN KEY (variant_id) REFERENCES catalog_variant(id)
);

-- ── Атрибуты и характеристики ────────────────────────────────────────────────

CREATE TABLE catalog_attribute (
  id          TEXT PRIMARY KEY,
  product_id  TEXT NOT NULL,
  name        TEXT NOT NULL,                                     -- Имя характеристики ("Цвет", "Объём")
  position    INTEGER DEFAULT 0,
  created_at  TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (product_id) REFERENCES catalog_product(id)
);

CREATE TABLE catalog_attribute_value (
  id           TEXT PRIMARY KEY,
  attribute_id TEXT NOT NULL,
  value        TEXT NOT NULL,                                    -- Конкретное значение ("Красный", "0.5л")
  position     INTEGER DEFAULT 0,
  created_at   TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (attribute_id) REFERENCES catalog_attribute(id)
);

CREATE TABLE variant_attribute (
  id           TEXT PRIMARY KEY,
  variant_id   TEXT NOT NULL,
  attribute_id TEXT NOT NULL,
  value_id     TEXT NOT NULL,
  FOREIGN KEY (attribute_id) REFERENCES catalog_attribute(id),
  FOREIGN KEY (value_id)     REFERENCES catalog_attribute_value(id)
);

CREATE TABLE composition_bom (
  id                 TEXT PRIMARY KEY,
  parent_variant_id  TEXT NOT NULL,                              -- Родительский SKU (набор)
  child_variant_id   TEXT NOT NULL,                              -- Дочерний SKU
  quantity           NUMERIC NOT NULL,
  created_at         TEXT DEFAULT (datetime('now')),
  unit               TEXT NOT NULL,                              -- ОКЕИ
  has_saled          INTEGER,                                    -- Продаётся ингредиентами?
  FOREIGN KEY (parent_variant_id) REFERENCES catalog_variant(id),
  FOREIGN KEY (child_variant_id)  REFERENCES catalog_variant(id)
);

-- ── Оргструктура ─────────────────────────────────────────────────────────────

CREATE TABLE legal_entity (
  id                TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  inn               TEXT UNIQUE NOT NULL,                        -- ИНН
  kpp               TEXT NOT NULL,
  address           TEXT NOT NULL,
  chief_accountant  TEXT,
  general_manager   TEXT,
  fsrar_id          TEXT NOT NULL,                               -- ФСРАР (12 цифр)
  fias_id           TEXT
);

CREATE TABLE shop (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  address         TEXT NOT NULL,
  legal_entity_id TEXT NOT NULL,
  timezone        TEXT DEFAULT 'Europe/Moscow',
  created_at      TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (legal_entity_id) REFERENCES legal_entity(id)
);

CREATE TABLE warehouse (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,                                     -- "Основной", "Брак"
  shop_id     TEXT NOT NULL,
  created_at  TEXT DEFAULT (datetime('now')),
  archived_at TEXT,
  FOREIGN KEY (shop_id) REFERENCES shop(id)
);

-- ── Контрагенты ──────────────────────────────────────────────────────────────

CREATE TABLE counterparty (
  id            TEXT PRIMARY KEY,
  created_at    TEXT NOT NULL,
  archived_at   TEXT,
  name          TEXT NOT NULL,
  full_name     TEXT NOT NULL,
  type          TEXT NOT NULL CHECK (type IN ('legal_entity','individual_entrepreneur','branch','individual')),
  inn           TEXT NOT NULL,
  kpp           TEXT,
  legal_address TEXT,
  is_supplier   INTEGER DEFAULT 1,
  is_customer   INTEGER DEFAULT 0,
  fsrar_id      TEXT NOT NULL,
  phone         TEXT,
  contact_name  TEXT,
  email         TEXT,
  fias_id       TEXT
);

CREATE TABLE edo_opertor (
  counterparty_id TEXT PRIMARY KEY,
  edo_id          TEXT NOT NULL,
  edo_opertor     TEXT CHECK (edo_opertor IN ('2BM','2BE','2AE','2VO','2PS','2LT','2AH','2LB','2BA','2HX','2JD','2BK','2LH','2AD','2LD','2LJ','2BV','2LG','2JM','2CL','2IJ','2CA','2IM','2MH')),
  edo_status      TEXT CHECK (edo_status IN ('REJECTED','ACCEPTED','INCOMING','NEW')),
  FOREIGN KEY (counterparty_id) REFERENCES counterparty(id)
);

CREATE TABLE counterparty_bank_account (
  id                    TEXT PRIMARY KEY,
  counterparty_id       TEXT NOT NULL,
  bank_name             TEXT NOT NULL,
  correspondent_account TEXT NOT NULL,
  bik                   TEXT NOT NULL,
  current_account       TEXT NOT NULL,
  is_main               INTEGER DEFAULT 0,
  FOREIGN KEY (counterparty_id) REFERENCES counterparty(id)
);

-- ── Документы: приходные накладные ───────────────────────────────────────────

CREATE TABLE doc_accept_invoice (
  id                   TEXT PRIMARY KEY,
  num                  TEXT NOT NULL,                            -- Номер из ЭДО/ЕГАИС
  created_at           TEXT NOT NULL,
  accepted_at          TEXT NOT NULL,                            -- Дата проведения
  archived_at          TEXT,
  warehouse_id         TEXT NOT NULL,
  store_id             TEXT NOT NULL,
  legal_entity_id      TEXT NOT NULL,                            -- Получатель (наше ЮЛ)
  contractor_id        TEXT NOT NULL,                            -- Поставщик
  source               TEXT NOT NULL CHECK (source IN ('EGAIS','EDO','MANUL')),
  status               TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','ACCEPTED','ARCHIVED')),
  positions_count      INTEGER DEFAULT 0,
  total_cost_with_tax  INTEGER DEFAULT 0,                        -- Сумма с НДС в копейках
  comment              TEXT,
  move_stock           INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (warehouse_id)    REFERENCES warehouse(id),
  FOREIGN KEY (store_id)        REFERENCES shop(id),
  FOREIGN KEY (legal_entity_id) REFERENCES legal_entity(id),
  FOREIGN KEY (contractor_id)   REFERENCES counterparty(id)
);

CREATE TABLE doc_position_accept_invoice (
  id                   INTEGER PRIMARY KEY,
  created_at           TEXT DEFAULT (datetime('now')),
  document_id          TEXT NOT NULL,
  variant_id           TEXT NOT NULL,
  name                 TEXT,
  quantity             NUMERIC NOT NULL,
  unit_cost_with_tax   INTEGER NOT NULL,                         -- Цена закупки за единицу в копейках
  total_cost_with_tax  INTEGER NOT NULL,                         -- quantity * unit_cost
  tax                  TEXT NOT NULL,                            -- NDS_*
  measure              TEXT NOT NULL,                            -- ОКЕИ
  article              TEXT,
  barcodes             TEXT,
  alc_type_code        TEXT,
  alc_code             TEXT,                                     -- JSON
  alc_capacity         TEXT,
  alc_volume           TEXT,
  alc_unitType         TEXT CHECK (alc_unitType IN ('PACKED','UNPAKED')),
  form_1               TEXT,
  form_2               TEXT,
  mark_group           TEXT,
  mark_codes           TEXT,
  FOREIGN KEY (document_id) REFERENCES doc_accept_invoice(id),
  FOREIGN KEY (variant_id)  REFERENCES catalog_variant(id)
);

CREATE TABLE doc_accept_invoice_gis_status (
  id            TEXT,
  document_id   TEXT,
  status        TEXT CHECK (status IN ('ERROR','PENDING','SUCCESS')),
  message       TEXT,
  source        TEXT CHECK (source IN ('EGAIS','EDO')),
  edo_status    TEXT CHECK (edo_status IN ('new','in_progress','wait_cancellation','receive_notice_required','error','cancel_confirmed','cancel_rejected','archived')),
  egais_status  TEXT CHECK (egais_status IN ('NEW','IN_PROGRESS','ARCHIVED')),
  original_id   TEXT
);

-- ── Складские документы ──────────────────────────────────────────────────────

CREATE TABLE doc_stock (
  id              TEXT PRIMARY KEY,
  num             TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  accepted_at     TEXT NOT NULL,
  archived_at     TEXT,
  warehouse_id    TEXT NOT NULL,
  store_id        TEXT NOT NULL,
  legal_entity_id TEXT NOT NULL,
  doc_type        TEXT NOT NULL CHECK (doc_type IN ('accept_invoice','writeoff','inventory','stock_entry','sale_receipt','refund_receipt','transfer','shift','price_change','accept_invoice_adjustment')),
  status          TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','ACCEPTED','ARCHIVED')),
  reason          TEXT,                                          -- Причина для writeoff
  comment         TEXT,
  move_stock      INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (warehouse_id)    REFERENCES warehouse(id),
  FOREIGN KEY (store_id)        REFERENCES shop(id),
  FOREIGN KEY (legal_entity_id) REFERENCES legal_entity(id)
);

CREATE TABLE doc_stock_positions (
  id                   INTEGER PRIMARY KEY,
  created_at           TEXT DEFAULT (datetime('now')),
  document_id          TEXT NOT NULL,
  variant_id           TEXT NOT NULL,
  name                 TEXT,
  type                 TEXT NOT NULL CHECK (type IN ('default','dish','service')),
  quantity             NUMERIC NOT NULL,
  unit_cost_with_tax   INTEGER NOT NULL,
  total_cost_with_tax  INTEGER NOT NULL,
  tax                  TEXT NOT NULL,
  measure              TEXT NOT NULL,
  article              TEXT,
  barcodes             TEXT,
  alc_type_code        TEXT,
  alc_code             TEXT,
  alc_capacity         TEXT,
  alc_volume           TEXT,
  alc_unitType         TEXT,
  form_1               TEXT,
  form_2               TEXT,
  mark_group           TEXT,
  mark_codes           TEXT,
  FOREIGN KEY (document_id) REFERENCES doc_stock(id),
  FOREIGN KEY (variant_id)  REFERENCES catalog_variant(id)
);

-- ── Перемещения ──────────────────────────────────────────────────────────────

CREATE TABLE doc_transfer (
  id                  TEXT PRIMARY KEY,
  num                 TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  accepted_at         TEXT NOT NULL,
  archived_at         TEXT,
  from_warehouse_id   TEXT NOT NULL,
  to_warehouse_id     TEXT NOT NULL,
  store_id            TEXT NOT NULL,
  legal_entity_id     TEXT NOT NULL,
  doc_type            TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'DRAFT',
  comment             TEXT,
  FOREIGN KEY (from_warehouse_id) REFERENCES warehouse(id),
  FOREIGN KEY (to_warehouse_id)   REFERENCES warehouse(id),
  FOREIGN KEY (store_id)          REFERENCES shop(id),
  FOREIGN KEY (legal_entity_id)   REFERENCES legal_entity(id)
);

CREATE TABLE doc_transfer_positions (
  id                 INTEGER PRIMARY KEY,
  created_at         TEXT DEFAULT (datetime('now')),
  document_id        TEXT NOT NULL,
  variant_id         TEXT NOT NULL,
  name               TEXT,
  type               TEXT NOT NULL,
  actual_quantity    NUMERIC NOT NULL,                           -- Факт на складе
  transfer_quantity  NUMERIC NOT NULL,                           -- К перемещению
  FOREIGN KEY (document_id) REFERENCES doc_transfer(id),
  FOREIGN KEY (variant_id)  REFERENCES catalog_variant(id)
);

-- ── Касса: смены, чеки ──────────────────────────────────────────────────────

CREATE TABLE doc_shift (
  id                  TEXT PRIMARY KEY,
  opened_at           TEXT NOT NULL,
  closed_at           TEXT,                                      -- Время Z-отчёта
  accepted_at         TEXT,
  archived_at         TEXT,
  shift_status        TEXT NOT NULL DEFAULT 'OPEN' CHECK (shift_status IN ('OPEN','CLOSED')),
  store_id            TEXT NOT NULL,
  legal_entity_id     TEXT NOT NULL,
  warehouse_id        TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'DRAFT',
  total_sales_sum     INTEGER DEFAULT 0,                         -- Сумма продаж (коп.)
  total_returns_sum   INTEGER DEFAULT 0,
  cash_in_sum         INTEGER DEFAULT 0,
  cash_out_sum        INTEGER DEFAULT 0,
  opening_cash        INTEGER NOT NULL,                          -- Стартовый налик (коп.)
  FOREIGN KEY (store_id)        REFERENCES shop(id),
  FOREIGN KEY (legal_entity_id) REFERENCES legal_entity(id),
  FOREIGN KEY (warehouse_id)    REFERENCES warehouse(id)
);

CREATE TABLE doc_receipt (
  id                    TEXT PRIMARY KEY,
  shift_id              TEXT NOT NULL,
  receipt_number        INTEGER NOT NULL,                        -- Номер чека в смене
  created_at            TEXT NOT NULL,                           -- Дата фискализации
  receipt_type          TEXT NOT NULL CHECK (receipt_type IN ('SALE','REFUND','OUTFLOW','OUTFLOW_REFUND','SALE_ANNUL','REFUND_ANNUL','ORDER','DELETED_ORDER','CORRECTION_INFLOW','CORRECTION_INFLOW_REFUND','CORRECTION_OUTFLOW','CORRECTION_OUTFLOW_REFUND')),
  total_sum             INTEGER DEFAULT 0,                       -- Итого с учётом скидок (коп.)
  sum_without_discounts INTEGER DEFAULT 0,
  discount_sum          INTEGER DEFAULT 0,
  cash_sum              INTEGER DEFAULT 0,                       -- Оплата налом
  cashless_sum          INTEGER DEFAULT 0,                       -- Оплата картой
  prepaid_sum           INTEGER DEFAULT 0,
  credit_sum            INTEGER DEFAULT 0,
  consideration_sum     INTEGER DEFAULT 0,
  cashier_name          TEXT,
  cashier_inn           TEXT,
  cashier_tab_number    TEXT,
  customer_phone        TEXT,
  customer_email        TEXT,
  tax_mode              TEXT NOT NULL CHECK (tax_mode IN ('DEFAULT','SIMPLE','SIMPLE_WO','ENVD','AGRICULT','PATENT')),
  number_fn             TEXT NOT NULL,                           -- Заводской номер ФН
  number_fd             TEXT NOT NULL,                           -- Номер ФД
  fiscal_sign           TEXT NOT NULL,                           -- ФПД
  registry_number       TEXT,
  kkt_inn               TEXT,
  move_stock            INTEGER NOT NULL DEFAULT 0,
  meta                  TEXT,                                    -- JSON: теги ОФД
  UNIQUE (number_fn, number_fd),
  FOREIGN KEY (shift_id) REFERENCES doc_shift(id)
);

CREATE TABLE doc_position_receipt (
  id              INTEGER PRIMARY KEY,
  document_id     TEXT NOT NULL,
  variant_id      TEXT NOT NULL,
  position_number INTEGER NOT NULL,
  quantity        INTEGER NOT NULL,                              -- Множитель x1000
  sale_price      INTEGER NOT NULL,                              -- Цена единицы (коп.)
  discount        INTEGER DEFAULT 0,
  tax             TEXT NOT NULL,
  goods_attribute INTEGER,                                       -- Реквизит 1212
  payment_type    INTEGER,
  settlement_type TEXT CHECK (settlement_type IN ('FULL','FULL_PREPAYMENT','PARTIAL_PREPAYMENT','ADVANCE','PARTIAL_AND_LOAN','LOAN','LOAN_REPAYMENT')),
  mark_code       TEXT,                                          -- Акцизная марка / марка ЧЗ
  mark_group      TEXT,
  barcode         TEXT,
  name            TEXT NOT NULL,
  type            TEXT NOT NULL,
  unit            TEXT NOT NULL,
  meta            TEXT,                                          -- JSON
  FOREIGN KEY (document_id) REFERENCES doc_receipt(id),
  FOREIGN KEY (variant_id)  REFERENCES catalog_variant(id)
);

-- ── Прочие документы ─────────────────────────────────────────────────────────

CREATE TABLE doc_price_change (
  id              TEXT PRIMARY KEY,
  num             TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  accepted_at     TEXT NOT NULL,
  archived_at     TEXT,
  legal_entity_id TEXT NOT NULL,
  doc_type        TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'DRAFT',
  comment         TEXT,
  FOREIGN KEY (legal_entity_id) REFERENCES legal_entity(id)
);

CREATE TABLE doc_price_change_positions (
  id              INTEGER PRIMARY KEY,
  created_at      TEXT DEFAULT (datetime('now')),
  document_id     TEXT NOT NULL,
  variant_id      TEXT NOT NULL,
  name            TEXT,
  cost_price      INTEGER,                                       -- Себестоимость (средневзв.)
  old_sale_price  INTEGER,
  new_sale_price  INTEGER,
  markup          NUMERIC,                                       -- Наценка %
  FOREIGN KEY (document_id) REFERENCES doc_price_change(id),
  FOREIGN KEY (variant_id)  REFERENCES catalog_variant(id)
);

CREATE TABLE doc_cash_operation (
  id                  TEXT PRIMARY KEY,
  shift_id            TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  cashier_name        TEXT NOT NULL,
  cashier_inn         TEXT,
  type                TEXT NOT NULL CHECK (type IN ('CASH_IN','CASH_OUT')),
  cashier_tab_number  TEXT,
  amount              INTEGER NOT NULL,                          -- Сумма в копейках
  reason              TEXT,
  FOREIGN KEY (shift_id) REFERENCES doc_shift(id)
);

CREATE TABLE document_dag (
  id              TEXT PRIMARY KEY,
  parent_doc_id   TEXT NOT NULL,
  child_doc_id    TEXT NOT NULL,
  parent_doc_type TEXT NOT NULL,
  child_doc_type  TEXT NOT NULL,
  created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE doc_inventory (
  id                      TEXT PRIMARY KEY,
  num                     TEXT NOT NULL,
  created_at              TEXT NOT NULL,
  accepted_at             TEXT NOT NULL,
  archived_at             TEXT,
  warehouse_id            TEXT NOT NULL,
  store_id                TEXT NOT NULL,
  legal_entity_id         TEXT NOT NULL,
  doc_type                TEXT NOT NULL,
  status                  TEXT NOT NULL DEFAULT 'DRAFT',
  comment                 TEXT,
  inventory_product_type  TEXT CHECK (inventory_product_type IN ('default','dish','service')),
  FOREIGN KEY (warehouse_id)    REFERENCES warehouse(id),
  FOREIGN KEY (store_id)        REFERENCES shop(id),
  FOREIGN KEY (legal_entity_id) REFERENCES legal_entity(id)
);

CREATE TABLE doc_inventory_positions (
  id            INTEGER PRIMARY KEY,
  created_at    TEXT DEFAULT (datetime('now')),
  document_id   TEXT NOT NULL,
  variant_id    TEXT NOT NULL,
  name          TEXT,
  type          TEXT NOT NULL,
  unit          TEXT NOT NULL,
  quantity      NUMERIC NOT NULL,                                -- Учётное
  quantity_ref  NUMERIC NOT NULL,                                -- Фактическое
  difference    INTEGER,
  mark_codes    TEXT,
  FOREIGN KEY (document_id) REFERENCES doc_inventory(id),
  FOREIGN KEY (variant_id)  REFERENCES catalog_variant(id)
);

-- ── Регистр остатков ─────────────────────────────────────────────────────────

CREATE TABLE stock_registry (
  id           TEXT PRIMARY KEY,
  document_id  TEXT NOT NULL,
  warehouse_id TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  doc_type     TEXT NOT NULL,
  op_type      TEXT NOT NULL CHECK (op_type IN ('IN','OUT')),
  variant_id   TEXT NOT NULL,
  quantity     NUMERIC NOT NULL,                                 -- ≥0; знак из op_type
  cost_price   INTEGER NOT NULL,
  sale_price   INTEGER NOT NULL,
  FOREIGN KEY (warehouse_id) REFERENCES warehouse(id),
  FOREIGN KEY (variant_id)   REFERENCES catalog_variant(id)
);

CREATE TABLE stock_counters (
  variant_id   TEXT NOT NULL,
  warehouse_id TEXT NOT NULL,
  quantity     NUMERIC NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (variant_id, warehouse_id),
  FOREIGN KEY (variant_id)   REFERENCES catalog_variant(id),
  FOREIGN KEY (warehouse_id) REFERENCES warehouse(id)
);

CREATE TABLE stock_daily_snapshots (
  variant_id    TEXT NOT NULL,
  warehouse_id  TEXT NOT NULL,
  snapshot_date TEXT NOT NULL,                                   -- YYYY-MM-DD-HH-MM-SS-MSS
  quantity      NUMERIC NOT NULL DEFAULT 0,
  PRIMARY KEY (variant_id, warehouse_id, snapshot_date),
  FOREIGN KEY (variant_id)   REFERENCES catalog_variant(id),
  FOREIGN KEY (warehouse_id) REFERENCES warehouse(id)
);

-- ── Индексы ──────────────────────────────────────────────────────────────────
CREATE INDEX idx_product_group         ON catalog_product (group_id);
CREATE INDEX idx_variant_product       ON catalog_variant (product_id);
CREATE INDEX idx_accept_legal          ON doc_accept_invoice (legal_entity_id);
CREATE INDEX idx_accept_created        ON doc_accept_invoice (created_at);
CREATE INDEX idx_pos_accept_doc        ON doc_position_accept_invoice (document_id);
CREATE INDEX idx_pos_accept_variant    ON doc_position_accept_invoice (variant_id);
CREATE INDEX idx_receipt_shift         ON doc_receipt (shift_id);
CREATE INDEX idx_receipt_created       ON doc_receipt (created_at);
CREATE INDEX idx_pos_receipt_doc       ON doc_position_receipt (document_id);
CREATE INDEX idx_reg_agg               ON stock_registry (warehouse_id, variant_id, created_at);
