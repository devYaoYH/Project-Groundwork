"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { ConfigDisclosure } from "./ConfigDisclosure";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { EnvironmentDetail as EnvironmentDetailData, Item, Parameter, getEnvironment, getItems, runOracle } from "../lib/api";

function domain(value: unknown[] | null) {
  if (!value) return "-";
  return Array.isArray(value) ? value.join(", ") : String(value);
}

// Design parameters show the range the release accepts. Item parameters show
// the strata the pinned bank actually holds, because that is what a factor over
// one can select — except when every item has its own value, where listing them
// would only restate the bank.
function levels(parameter: Parameter, itemCount: number, href: string) {
  if (parameter.source !== "item") return domain(parameter.domain);
  if (parameter.identity_grained) {
    return <Link className="text-link" href={href}>{itemCount} items</Link>;
  }
  return (parameter.levels ?? [])
    .map((level) => `${String(level.value)} (${level.count})`)
    .join("  ·  ");
}

export function EnvironmentDetail({ id }: { id: string }) {
  const [detail, setDetail] = useState<EnvironmentDetailData | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [selectedItem, setSelectedItem] = useState("");
  const [oracle, setOracle] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getEnvironment(id), getItems(id)])
      .then(([nextDetail, itemPage]) => {
        setDetail(nextDetail);
        setItems(itemPage.items);
        setSelectedItem(itemPage.items[0]?.item_id ?? "");
      })
      .catch((reason: Error) => setError(reason.message));
  }, [id]);

  async function handleOracle() {
    if (!selectedItem) return;
    setOracle(null);
    try {
      const result = await runOracle(id, selectedItem);
      setOracle(JSON.stringify(result.result, null, 2));
    } catch (reason) {
      setOracle(reason instanceof Error ? reason.message : "Unable to run oracle");
    }
  }

  const title = detail?.environment_id ?? id;
  return (
    <>
      <Crumb items={[{ label: "environments", href: "/environments/" }, { label: title }]} />
      <header className="page-heading page-heading-split">
        <div>
          <h1>{title}</h1>
          <p>{detail?.release.version ? `Release ${detail.release.version}. ` : ""}{detail ? "The declaration is the boundary between source code and research design." : "Loading release declaration..."}</p>
        </div>
        {detail ? <Link className="button" href={`/environments/${id}/items/`}>Item bank</Link> : null}
      </header>
      {error ? <p className="notice notice-error">{error}</p> : null}
      {detail ? (
        <>
          <section className="card section-card">
            <div className="section-heading"><h2>Parameters</h2><p>Design-set levels are written into the episode config, while item levels are frozen in the bank alongside the oracle — factoring over one selects items rather than overriding them.</p></div>
            <DataTable
              rows={detail.parameters}
              rowKey={(parameter) => parameter.name}
              columns={[
                { label: "name", className: "mono", render: (parameter) => parameter.name },
                { label: "type", render: (parameter) => parameter.type },
                { label: "levels", className: "mono muted", render: (parameter) => levels(parameter, detail.item_policy.item_count, `/environments/${id}/items/`) },
                { label: "source", render: (parameter) => parameter.source === "item" ? <Chip>from items</Chip> : <Chip tone="good">design-set</Chip> },
                { label: "release", render: (parameter) => parameter.fixed ? <Chip tone="warn">fixed</Chip> : <span className="muted">design-open</span> },
              ]}
            />
          </section>
          <section className="card section-card">
            <div className="section-heading"><h2>Measures</h2><p>Declared now for release review; aggregate analysis is a later layer.</p></div>
            <DataTable
              rows={detail.measures}
              rowKey={(measure) => measure.name}
              columns={[
                { label: "name", className: "mono", render: (measure) => measure.name },
                { label: "producer", render: (measure) => measure.producer },
                { label: "grain", render: (measure) => measure.grain },
                { label: "index", className: "mono muted", render: (measure) => measure.index_label ?? "-" },
                { label: "unit", render: (measure) => measure.unit },
                { label: "direction", render: (measure) => measure.direction },
              ]}
            />
          </section>
          <section className="card section-card oracle-card">
            <div className="section-heading"><h2>Oracle</h2><p>{detail.release.oracle_version ? `Pinned as ${detail.release.oracle_version}.` : "This release does not publish an oracle."}</p></div>
            <div className="oracle-controls">
              <label>
                <span>Item</span>
                <select value={selectedItem} onChange={(event) => setSelectedItem(event.target.value)}>
                  {items.map((item) => <option key={item.item_id} value={item.item_id}>{item.item_id}</option>)}
                </select>
              </label>
              <button className="button button-primary" onClick={handleOracle} disabled={!detail.release.oracle_version || !selectedItem}>Run oracle</button>
              <Link className="text-link" href={`/environments/${id}/items/`}>Inspect all items</Link>
            </div>
            {oracle ? <pre className="oracle-result">{oracle}</pre> : null}
          </section>
          <section className="release-meta">
            <span>release <strong className="mono">{detail.release.release_id}</strong></span>
            <span>declaration <strong className="mono">{detail.release.declaration_sha256}</strong></span>
            <span>item bank <strong className="mono">{detail.release.item_bank_sha256}</strong></span>
          </section>
          <ConfigDisclosure value={detail.declaration_yaml} />
        </>
      ) : null}
    </>
  );
}
