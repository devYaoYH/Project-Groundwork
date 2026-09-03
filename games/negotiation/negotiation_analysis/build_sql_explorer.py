"""Builds a standalone HTML SQL explorer for Negotiation Environment episodes.

Loads data using the modernized NegotiationDataset and embeds it into an 
interactive HTML page powered by DuckDB-WASM.
"""

import json
import argparse
import sys
from pathlib import Path
import pandas as pd
import numpy as np
from negotiation_analysis.data_loader import load_dataset

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Negotiation Trace SQL Explorer</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { font-family: system-ui, -apple-system, sans-serif; background: #f8f9fa; }
        .container-fluid { padding: 2rem; }
        #editor { height: 200px; width: 100%; font-family: monospace; padding: 10px; border: 1px solid #ccc; border-radius: 4px; }
        .table-container { height: 600px; overflow: auto; background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .status { font-size: 0.8rem; color: #666; }
        pre { background: #eee; padding: 10px; border-radius: 4px; font-size: 0.85rem; }
        .btn-copied { transition: background-color 0.3s; }
        .alert-cors { display: none; margin-bottom: 1rem; }
    </style>
</head>
<body>
    <div class="container-fluid">
        <h2 class="mb-4">Negotiation Trace SQL Explorer</h2>
        
        <div id="cors-warning" class="alert alert-warning alert-cors">
            <strong>Warning:</strong> You are opening this file via <code>file://</code>. 
            DuckDB-WASM requires a web server to load. <br>
            Please run: <code>python3 -m http.server 8000</code> in this directory and open 
            <a href="http://localhost:8000/scripts/analysis/sql_explorer.html">http://localhost:8000/scripts/analysis/sql_explorer.html</a>
        </div>

        <div class="row">
            <div class="col-md-4">
                <div class="card mb-4">
                    <div class="card-header">Schema Browser</div>
                    <div class="card-body">
                        <h6>Tables:</h6>
                        <ul id="table-list">
                        </ul>
                        <div class="status" id="db-status">Initializing DuckDB...</div>
                    </div>
                </div>

                <div class="card">
                    <div class="card-header">Query Presets</div>
                    <div class="card-body" id="presets">
                        <button class="btn btn-sm btn-outline-secondary w-100 mb-2" data-sql="SELECT * FROM games LIMIT 10">Select All Games</button>
                        <button class="btn btn-sm btn-outline-secondary w-100 mb-2" data-sql="SELECT pair, AVG(joint_efficiency) as avg_eff FROM games GROUP BY pair ORDER BY avg_eff DESC">Efficiency by Model Pair</button>
                        <button class="btn btn-sm btn-outline-secondary w-100 mb-2" data-sql="SELECT round_number, AVG(joint_efficiency) FROM rounds GROUP BY round_number ORDER BY round_number">Efficiency by Round</button>
                        <button class="btn btn-sm btn-outline-secondary w-100 mb-2" data-sql="SELECT model, is_shifted, AVG(fair_efficiency) as avg_fair_eff, AVG(individual_share) as avg_share, COUNT(*) as n FROM agents GROUP BY model, is_shifted ORDER BY model, is_shifted">Fair Efficiency by Model × Shifted</button>
                        <button class="btn btn-sm btn-outline-secondary w-100" data-sql="SELECT speaker, AVG(message_len) FROM turns WHERE message_len > 0 GROUP BY speaker">Msg Length by Speaker</button>
                    </div>
                </div>
            </div>

            <div class="col-md-8">
                <textarea id="editor">SELECT * FROM games LIMIT 100</textarea>
                <div class="d-flex gap-2 mt-2 mb-3">
                    <button id="run-btn" class="btn btn-primary flex-grow-1">Run SQL Query (Ctrl+Enter)</button>
                    <button id="copy-btn" class="btn btn-outline-secondary" disabled>Copy CSV</button>
                </div>
                
                <div id="result-meta" class="status mb-2"></div>
                <div class="table-container">
                    <table id="result-table" class="table table-hover table-sm">
                        <thead class="sticky-top bg-white"></thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script type="application/json" id="data-json">{DATA_JSON}</script>

    <script type="module">
        import * as duckdb from 'https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.28.0/+esm';

        let db;
        let conn;
        let lastResult = null;

        if (window.location.protocol === 'file:') {
            document.getElementById('cors-warning').style.display = 'block';
        }

        async function init() {
            try {
                const JSDELIVR_BUNDLES = duckdb.getJsDelivrBundles();
                const bundle = await duckdb.selectBundle(JSDELIVR_BUNDLES);
                const worker = await duckdb.createWorker(bundle.mainWorker);
                const logger = new duckdb.ConsoleLogger();
                db = new duckdb.AsyncDuckDB(logger, worker);
                await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
                conn = await db.connect();
                
                // Enable extension auto-loading for JSON support
                await conn.query(`SET autoinstall_known_extensions=1; SET autoload_known_extensions=1;`);
                
                document.getElementById('db-status').innerText = 'Loading data...';
                
                const dataText = document.getElementById('data-json').textContent;
                const data = JSON.parse(dataText);
                const tableList = document.getElementById('table-list');
                
                for (const [tableName, tableData] of Object.entries(data)) {
                    const csvData = tableData.csv;
                    const schema = tableData.schema;
                    const rowCount = tableData.rowCount;
                    
                    // Add to UI list
                    const li = document.createElement('li');
                    li.innerHTML = `<code>${tableName}</code> (${rowCount} rows)`;
                    tableList.appendChild(li);

                    if (rowCount === 0) {
                        // Create empty table
                        const colDefs = Object.entries(schema).map(([name, type]) => `"${name}" ${type}`).join(', ');
                        await conn.query(`CREATE TABLE "${tableName}" (${colDefs})`);
                        continue;
                    }

                    // Register CSV as a file
                    const fileName = `${tableName}.csv`;
                    await db.registerFileText(fileName, csvData);
                    
                    // Map schema to columns hint for read_csv_auto
                    const columnTypes = Object.entries(schema)
                        .map(([name, type]) => `'${name}': '${type}'`)
                        .join(', ');

                    await conn.query(`CREATE TABLE "${tableName}" AS SELECT * FROM read_csv_auto('${fileName}', columns={${columnTypes}}, header=True)`);
                    
                    // Cleanup file after loading
                    await db.registerFileText(fileName, '');
                }

                document.getElementById('db-status').innerText = '✓ DuckDB Ready';
                runQuery();
            } catch (e) {
                console.error(e);
                document.getElementById('db-status').innerText = 'Failed to init: ' + e.message;
            }
        }

        async function runQuery() {
            const sql = document.getElementById('editor').value;
            const start = performance.now();
            try {
                const result = await conn.query(sql);
                const end = performance.now();
                lastResult = result;
                document.getElementById('copy-btn').disabled = false;
                renderResult(result, end - start);
            } catch (e) {
                document.getElementById('result-meta').innerText = 'Error: ' + e.message;
            }
        }

        function renderResult(result, duration) {
            const table = document.getElementById('result-table');
            const thead = table.querySelector('thead');
            const tbody = table.querySelector('tbody');
            thead.innerHTML = '';
            tbody.innerHTML = '';

            if (result.numRows === 0) {
                document.getElementById('result-meta').innerText = `0 rows returned (${duration.toFixed(2)}ms)`;
                return;
            }

            const headerRow = document.createElement('tr');
            for (const col of result.schema.fields) {
                const th = document.createElement('th');
                th.innerText = col.name;
                headerRow.appendChild(th);
            }
            thead.appendChild(headerRow);

            const rows = result.toArray();
            for (let i = 0; i < Math.min(rows.length, 500); i++) {
                const tr = document.createElement('tr');
                const row = rows[i];
                for (const field of result.schema.fields) {
                    const td = document.createElement('td');
                    let val = row[field.name];
                    if (typeof val === 'object' && val !== null) {
                        if (typeof val === 'bigint') val = val.toString();
                        else val = JSON.stringify(val);
                    }
                    td.innerText = val;
                    tr.appendChild(td);
                }
                tbody.appendChild(tr);
            }

            let metaText = `${rows.length} rows returned (${duration.toFixed(2)}ms)`;
            if (rows.length > 500) metaText += ' [Showing first 500]';
            document.getElementById('result-meta').innerText = metaText;
        }

        document.getElementById('copy-btn').addEventListener('click', () => {
            if (!lastResult || lastResult.numRows === 0) return;
            const cols = lastResult.schema.fields.map(f => f.name);
            const rows = lastResult.toArray();
            const csvRows = [cols.join(',')];
            for (const row of rows) {
                csvRows.push(cols.map(c => {
                    let v = row[c];
                    if (v === null || v === undefined) return '';
                    if (typeof v === 'bigint') v = v.toString();
                    else if (typeof v === 'object') v = JSON.stringify(v);
                    v = String(v);
                    if (v.includes(',') || v.includes('"') || v.includes('\\n'))
                        return '"' + v.replace(/"/g, '""') + '"';
                    return v;
                }).join(','));
            }
            navigator.clipboard.writeText(csvRows.join('\\n')).then(() => {
                const btn = document.getElementById('copy-btn');
                btn.innerText = 'Copied!';
                btn.classList.add('btn-success');
                btn.classList.remove('btn-outline-secondary');
                setTimeout(() => { btn.innerText = 'Copy CSV'; btn.classList.remove('btn-success'); btn.classList.add('btn-outline-secondary'); }, 1500);
            });
        });
        document.getElementById('run-btn').addEventListener('click', runQuery);
        document.getElementById('editor').addEventListener('keydown', (e) => {
            if (e.ctrlKey && e.key === 'Enter') runQuery();
        });
        document.getElementById('presets').addEventListener('click', (e) => {
            if (e.target.tagName === 'BUTTON') {
                document.getElementById('editor').value = e.target.getAttribute('data-sql');
                runQuery();
            }
        });

        init();
    </script>
</body>
</html>
"""

def get_sql_type(dtype):
    """Map pandas dtype to DuckDB SQL type."""
    dstr = str(dtype).lower()
    if "int" in dstr: return "INTEGER"
    if "float" in dstr: return "DOUBLE"
    if "bool" in dstr: return "BOOLEAN"
    return "VARCHAR"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", help="Filter by experiment run ID")
    parser.add_argument("--schema", type=int, default=7, help="Filter by schema version")
    parser.add_argument("--output", default="scripts/analysis/sql_explorer.html")
    args = parser.parse_args()

    print(f"Loading dataset (schema_version >= {args.schema})...")
    dataset = load_dataset(schema_version=args.schema, run_id=args.run_id)
    
    if not dataset.games:
        print("No games found. Try a different schema version or run-id.")
        return

    print(f"Normalizing {len(dataset.games)} games...")
    
    dfs = {
        "games": dataset.to_game_df(),
        "rounds": dataset.to_round_df(),
        "agents": dataset.to_agent_df(),
        "turns": dataset.to_turn_df(),
    }

    data = {}
    for name, df in dfs.items():
        print(f"  - {name}: {len(df)} rows, {len(df.columns)} columns")
        if len(df.columns) == 0:
            continue
            
        # Handle NaN/Inf for CSV transport at DataFrame level
        # pd.NA/np.nan will become empty string in CSV
        data[name] = {
            "schema": {col: get_sql_type(df.dtypes[col]) for col in df.columns},
            "csv": df.to_csv(index=False),
            "rowCount": len(df)
        }

    print(f"Embedding data into {args.output}...")
    json_data = json.dumps(data, default=str)
    html_content = HTML_TEMPLATE.replace("{DATA_JSON}", json_data)
    
    with open(args.output, "w") as f:
        f.write(html_content)
    
    print(f"Done! Open {args.output} in your browser to start exploring.")

if __name__ == "__main__":
    main()
