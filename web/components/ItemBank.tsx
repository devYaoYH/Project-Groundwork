"use client";

import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { Item, getItems } from "../lib/api";

export function ItemBank({ id }: { id: string }) {
  const [items, setItems] = useState<Item[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [digest, setDigest] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    getItems(id)
      .then((page) => {
        setItems(page.items ?? []);
        setCursor(page.next_cursor ?? null);
        setDigest(page.item_bank_sha256 ?? null);
      })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoaded(true));
  }, [id]);

  async function loadMore() {
    if (!cursor) return;
    const page = await getItems(id, cursor);
    setItems((current) => [...current, ...(page.items ?? [])]);
    setCursor(page.next_cursor ?? null);
  }

  return (
    <>
      <Crumb items={[{ label: "environments", href: "/environments/" }, { label: id, href: `/environments/${id}/` }, { label: "items" }]} />
      <header className="page-heading">
        <div>
          <h1>Item bank</h1>
          <p>Frozen release input. Items remain identifiable by their content-addressed bank, not their filename.</p>
        </div>
      </header>
      {error ? <p className="notice notice-error">{error}</p> : null}
      {digest ? <p className="bank-digest">bank digest <strong className="mono">{digest}</strong></p> : null}
      <section className="card">
        {items.length === 0 && !error ? (
          <p className="empty-state">{loaded ? "This release ships an empty item bank." : "Loading item bank..."}</p>
        ) : null}
        {items.length > 0 ? (
          <DataTable
            rows={items}
            rowKey={(item) => item.item_id}
            columns={[
              { label: "item", className: "mono", render: (item) => item.item_id },
              { label: "item parameters", className: "item-params", render: (item) => <code>{JSON.stringify(item.params)}</code> },
              { label: "oracle", render: (item) => item.oracle_result === null ? <span className="muted">not available</span> : <Chip tone="good">available</Chip> },
            ]}
          />
        ) : null}
      </section>
      {cursor ? <button className="button" onClick={loadMore}>Load more items</button> : null}
    </>
  );
}
