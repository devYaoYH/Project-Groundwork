import { ReactNode } from "react";

export type Column<Row> = {
  label: string;
  render: (row: Row) => ReactNode;
  className?: string;
};

export function DataTable<Row>({ rows, columns, rowKey }: {
  rows: Row[];
  columns: Column<Row>[];
  rowKey: (row: Row) => string;
}) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>{columns.map((column) => <th className={column.className} key={column.label}>{column.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((column) => <td className={column.className} key={column.label}>{column.render(row)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
