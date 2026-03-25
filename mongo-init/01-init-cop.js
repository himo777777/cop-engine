// ============================================================
// COP Engine — MongoDB Initialization
// ============================================================
// Körs automatiskt vid första docker compose up
// Skapar index och exempeldata för utveckling
// ============================================================

db = db.getSiblingDB('meteor');

// === Collections & Index ===

// Shifts (schemaposterna)
db.createCollection('shifts');
db.shifts.createIndex({ "doctor_id": 1, "date": 1 }, { unique: true });
db.shifts.createIndex({ "date": 1 });
db.shifts.createIndex({ "source": 1 });

// Staff (personalregister)
db.createCollection('staff');
db.staff.createIndex({ "employee_id": 1 }, { unique: true });
db.staff.createIndex({ "department": 1 });

// Sync log
db.createCollection('cop_sync_log');
db.cop_sync_log.createIndex({ "timestamp": -1 });
db.cop_sync_log.createIndex({ "direction": 1, "timestamp": -1 });

print("✅ COP MongoDB initialized — collections and indexes created");
