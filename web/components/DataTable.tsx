import { ReactNode } from "react";

export type Column<Row> = {
  label: string;
  render: (row: Row) => ReactNode;
  className?: string;
};

export function DataTable<Row>({ rows, columns, rowKey }: {
  // Nullable because it is fed straight from API responses: an endpoint that
  // answers with a missing or null collection must render an empty table
  // rather than take the page down.
  rows: Row[] | null | undefined;
  columns: Column<Row>[];
  rowKey: (row: Row) => string;
}) {
  const safeRows = Array.isArray(rows) ? rows : [];
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>{columns.map((column) => <th className={column.className} key={column.label}>{column.label}</th>)}</tr>
        </thead>
        <tbody>
          {safeRows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((column) => <td className={column.className} key={column.label}>{column.render(row)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
