/**
 * Run all ClickHouse unclustered migrations from within the Langfuse container.
 * Usage: docker cp this_file container:/tmp/ && docker exec container node /tmp/run_clickhouse_migrations.js
 */
const http = require('http');
const fs = require('fs');
const path = require('path');

const MIGRATIONS_DIR = '/app/packages/shared/clickhouse/migrations/unclustered';
const CH_HOST = 'langfuse-clickhouse';
const CH_PORT = 8123;
const CH_DB = 'langfuse';
const CH_USER = 'default';
const CH_PASS = 'ch_password';

function runSQL(sql) {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: CH_HOST,
      port: CH_PORT,
      path: `/?database=${CH_DB}&user=${CH_USER}&password=${CH_PASS}`,
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
    };
    const req = http.request(options, (res) => {
      let data = '';
      res.on('data', (c) => (data += c));
      res.on('end', () => {
        if (res.statusCode >= 400) {
          reject(new Error(`HTTP ${res.statusCode}: ${data.substring(0, 200)}`));
        } else {
          resolve(data);
        }
      });
    });
    req.on('error', (e) => reject(e));
    req.write(sql);
    req.end();
  });
}

function splitSQL(sql) {
  // Split by semicolons, respecting quotes and backticks
  const statements = [];
  let current = '';
  let inSingleQuote = false;
  let inDoubleQuote = false;
  let inBacktick = false;

  for (let i = 0; i < sql.length; i++) {
    const ch = sql[i];

    if (ch === "'" && !inDoubleQuote && !inBacktick) inSingleQuote = !inSingleQuote;
    else if (ch === '"' && !inSingleQuote && !inBacktick) inDoubleQuote = !inDoubleQuote;
    else if (ch === '`' && !inSingleQuote && !inDoubleQuote) inBacktick = !inBacktick;

    if (ch === ';' && !inSingleQuote && !inDoubleQuote && !inBacktick) {
      const stmt = current.trim();
      if (stmt && !stmt.startsWith('--')) statements.push(stmt);
      current = '';
    } else {
      current += ch;
    }
  }
  const stmt = current.trim();
  if (stmt && !stmt.startsWith('--')) statements.push(stmt);
  return statements;
}

// Expected errors that are safe to ignore (idempotent DDL)
const IGNORABLE_ERRORS = [
  'UNKNOWN_TABLE',
  'NOT_IMPLEMENTED',
  "Can't drop",
  "doesn't exist",
  'BAD_GET',
  'Directory already exists',
  'TABLE_ALREADY_EXISTS',
  'ALREADY_EXISTS',
  'INDEX_ALREADY_EXISTS',
  'MATERIALIZED_VIEW_ALREADY_EXISTS',
];

function isIgnorable(errMsg) {
  return IGNORABLE_ERRORS.some((pattern) => errMsg.includes(pattern));
}

async function main() {
  const files = fs
    .readdirSync(MIGRATIONS_DIR)
    .filter((f) => f.endsWith('.up.sql'))
    .sort();

  console.log(`Found ${files.length} migration files\n`);

  let totalOk = 0;
  let totalFail = 0;
  const failures = [];

  for (const file of files) {
    const sql = fs.readFileSync(path.join(MIGRATIONS_DIR, file), 'utf8');
    const stmts = splitSQL(sql);
    let fileOk = true;

    for (let i = 0; i < stmts.length; i++) {
      try {
        await runSQL(stmts[i]);
      } catch (err) {
        if (!isIgnorable(err.message)) {
          console.log(`  FAIL stmt[${i}]: ${err.message.substring(0, 150)}`);
          fileOk = false;
        }
      }
    }

    if (fileOk) {
      console.log(`OK: ${file} (${stmts.length} stmts)`);
      totalOk++;
    } else {
      console.log(`FAIL: ${file}`);
      failures.push(file);
      totalFail++;
    }
  }

  console.log(`\n=== ${totalOk} OK, ${totalFail} FAIL ===`);

  if (failures.length > 0) {
    console.log('\nFailed migrations:');
    failures.forEach((f) => console.log(`  - ${f}`));
  }

  // Show created tables
  const tables = await runSQL('SHOW TABLES');
  console.log('\nTables in langfuse:');
  console.log(tables);
}

main().catch((e) => console.error('FATAL:', e));
