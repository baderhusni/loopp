import { useEffect, useState } from "react";
import { getCustomers } from "../api";
import type { Customer } from "../types";

export default function CrmExplorer() {
  const [customers, setCustomers] = useState<Customer[] | null>(null);

  useEffect(() => {
    getCustomers().then((d) => setCustomers(d.customers)).catch(() => setCustomers([]));
  }, []);

  if (!customers) return <div className="loading">Loading CRM…</div>;

  return (
    <div className="crm-scroll">
      {customers.map((c) => (
        <div className="crm-customer" key={c.id}>
          <div className="crm-customer-head">
            <span className="name">{c.name}</span>
            <span className="email">{c.email}</span>
            <span className="muted" style={{ fontSize: 11 }}>
              {c.id} · {c.tier}
            </span>
          </div>
          {c.orders.map((o) => (
            <table className="items" key={o.id}>
              <thead>
                <tr>
                  <th>{o.id} · {o.status}{o.delivered_at ? ` · delivered ${o.delivered_at}` : ""}</th>
                  <th>item</th>
                  <th>price</th>
                  <th>flags</th>
                </tr>
              </thead>
              <tbody>
                {o.items.map((it) => (
                  <tr key={it.id}>
                    <td className="muted">{it.id}</td>
                    <td>{it.name}</td>
                    <td>${(it.unit_price * it.quantity).toFixed(2)}</td>
                    <td>
                      {it.final_sale && <span className="flag final">final sale</span>}{" "}
                      {it.refunded && <span className="flag refunded">refunded</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
        </div>
      ))}
    </div>
  );
}
